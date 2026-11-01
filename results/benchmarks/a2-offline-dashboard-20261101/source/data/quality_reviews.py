"""Bind attributable human rubric judgments to immutable calibration outputs.

These records attest what a named reviewer supplied; hashes bind content, not
reviewer identity. This module never generates judgments or contacts a model.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re


DEFAULT_RUBRIC_PATH = Path(__file__).with_name("workloads") / "quality_rubric.v1.json"
_STEPS = frozenset({"classify", "retrieve", "draft", "summarize"})
_REVIEW_FIELDS = frozenset({
    "schema_version", "record_type", "trial_id", "trial_sha256", "output_sha256",
    "case_id", "split", "step_id", "rubric_id", "rubric_sha256", "status",
    "review_method", "reviewer", "reviewed_at", "criteria", "score", "rationale",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _json_safe(value):
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_safe(item)
        return
    if isinstance(value, dict) and all(type(key) is str for key in value):
        for item in value.values():
            _json_safe(item)
        return
    raise ValueError("Review evidence must contain only finite JSON values")


def fingerprint(value):
    """SHA256 of strict canonical JSON, including Unicode and all supplied fields."""
    _json_safe(value)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def load_json(path):
    """Load UTF-8 JSON, rejecting duplicate object fields and non-finite numbers."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field in review evidence")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("Review evidence requires finite JSON numbers")

    value = json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      object_pairs_hook=unique, parse_constant=nonfinite)
    _json_safe(value)  # JSON exponent overflow is not handled by parse_constant.
    return value


def _validate_rubric(rubric):
    if not isinstance(rubric, dict) or set(rubric) != {
        "schema_version", "record_type", "rubric_id", "description", "scoring", "steps",
    }:
        raise ValueError("Invalid QualityRubric fields")
    if type(rubric["schema_version"]) is not int or rubric["schema_version"] != 1:
        raise ValueError("Unsupported QualityRubric schema_version")
    if rubric["record_type"] != "QualityRubric":
        raise ValueError("Expected QualityRubric")
    for name in ("rubric_id", "description", "scoring"):
        _text(rubric[name], name)
    if not isinstance(rubric["steps"], dict) or set(rubric["steps"]) != _STEPS:
        raise ValueError("Rubric must cover all four workflow steps")
    for step, criteria in rubric["steps"].items():
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("Each rubric step requires criteria")
        ids = set()
        for criterion in criteria:
            if not isinstance(criterion, dict) or set(criterion) != {"id", "description"}:
                raise ValueError("Invalid rubric criterion fields")
            _text(criterion["id"], "criterion id")
            _text(criterion["description"], "criterion description")
            if criterion["id"] in ids:
                raise ValueError(f"Duplicate criterion id for {step}")
            ids.add(criterion["id"])
    _json_safe(rubric)


def load_rubric(path=None):
    rubric = load_json(DEFAULT_RUBRIC_PATH if path is None else path)
    _validate_rubric(rubric)
    return rubric


def _eligible(trial):
    """Execution/format eligibility does not establish semantic quality."""
    return (type(trial.get("schema_version")) is int and trial["schema_version"] == 1
            and trial.get("record_type") == "CalibrationTrial"
            and trial.get("split") == "calibration" and trial.get("warmup") is False
            and trial.get("status") == "completed" and trial.get("output_valid") is True
            and trial.get("completion_reason") == "stop"
            and isinstance(trial.get("step_id"), str) and trial["step_id"] in _STEPS
            and isinstance(trial.get("result"), dict)
            and isinstance(trial["result"].get("text"), str)
            and bool(trial["result"]["text"].strip()))


def _reviewable(trial):
    if not isinstance(trial, dict) or not _eligible(trial):
        raise ValueError("Only valid natural calibration completions outside warm-up can train quality")
    for name in ("trial_id", "case_id", "prompt"):
        _text(trial.get(name), name)
    if (trial.get("quality_score") is not None
            or trial.get("quality_review_fingerprint") is not None
            or str(trial.get("quality_source", "unknown")).startswith(("human-review", "manual-review"))):
        raise ValueError("Existing quality judgments cannot be overwritten")
    _json_safe(trial)


def _bindings(trial, rubric):
    return {
        "trial_id": trial["trial_id"], "trial_sha256": fingerprint(trial),
        "output_sha256": hashlib.sha256(trial["result"]["text"].encode("utf-8")).hexdigest(),
        "case_id": trial["case_id"], "split": trial["split"], "step_id": trial["step_id"],
        "rubric_id": rubric["rubric_id"], "rubric_sha256": fingerprint(rubric),
    }


def review_template(trial, rubric):
    """Create a pending record. Every judgment must be entered by its reviewer."""
    _validate_rubric(rubric)
    _reviewable(trial)
    return {
        "schema_version": 1, "record_type": "QualityReview", **_bindings(trial, rubric),
        "status": "pending", "review_method": None, "reviewer": None, "reviewed_at": None,
        "criteria": {criterion["id"]: {"score": None, "rationale": ""}
                     for criterion in rubric["steps"][trial["step_id"]]},
        "score": None, "rationale": "",
    }


def _score(value):
    if type(value) not in {int, float}:
        raise ValueError("Review score must be a finite number from zero to one")
    try:
        valid = math.isfinite(value) and 0 <= value <= 1
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Review score must be a finite number from zero to one")


def _validate_review(review, trial, rubric):
    _reviewable(trial)
    if not isinstance(review, dict) or set(review) != _REVIEW_FIELDS:
        raise ValueError("Invalid QualityReview fields")
    if type(review["schema_version"]) is not int or review["schema_version"] != 1:
        raise ValueError("Unsupported QualityReview schema_version")
    if review["record_type"] != "QualityReview":
        raise ValueError("Expected QualityReview")
    if review["status"] != "approved" or review["review_method"] != "human":
        raise ValueError("Only approved human reviews may supply profile quality")
    for key, value in _bindings(trial, rubric).items():
        if review[key] != value:
            raise ValueError(f"Review {key} differs from its immutable evidence")
    for key in ("trial_sha256", "output_sha256", "rubric_sha256"):
        if not isinstance(review[key], str) or not _SHA256.fullmatch(review[key]):
            raise ValueError("Review requires SHA256 content bindings")
    _text(review["reviewer"], "reviewer")
    _text(review["rationale"], "review rationale")
    _text(review["reviewed_at"], "reviewed_at")
    try:
        instant = datetime.fromisoformat(review["reviewed_at"])
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Review timestamp must be timezone-aware")
        if instant.astimezone(timezone.utc) > datetime.now(timezone.utc):
            raise ValueError("Review timestamp cannot be in the future")
    except (ValueError, OverflowError) as exc:
        raise ValueError("Review timestamp must be timezone-aware and not in the future") from exc
    if "finished_at" in trial:
        _text(trial["finished_at"], "trial finished_at")
        try:
            finished = datetime.fromisoformat(trial["finished_at"])
            if finished.tzinfo is None or finished.utcoffset() is None:
                raise ValueError("Trial completion timestamp must be timezone-aware")
            if instant < finished:
                raise ValueError("Review cannot precede trial completion")
        except (ValueError, OverflowError) as exc:
            raise ValueError("Review must follow a valid timezone-aware trial completion") from exc
    expected = {criterion["id"] for criterion in rubric["steps"][trial["step_id"]]}
    if not isinstance(review["criteria"], dict) or set(review["criteria"]) != expected:
        raise ValueError("Review criteria differ from the rubric")
    scores = []
    for judgment in review["criteria"].values():
        if not isinstance(judgment, dict) or set(judgment) != {"score", "rationale"}:
            raise ValueError("Invalid criterion judgment fields")
        _score(judgment["score"])
        _text(judgment["rationale"], "criterion rationale")
        scores.append(judgment["score"])
    _score(review["score"])
    if not math.isclose(review["score"], math.fsum(scores) / len(scores), rel_tol=0, abs_tol=1e-12):
        raise ValueError("Review aggregate score must equal the mean criterion score")
    _json_safe(review)


def apply_reviews(trials, reviews, *, rubric):
    """Validate reviews, enrich copies, and return complete deterministic evidence.

    One explicit approved judgment per trial is supported. Multi-reviewer
    adjudication must produce a new attributable decision outside this function.
    An empty review set preserves unknown quality and all original trial fields.
    """
    _validate_rubric(rubric)
    if not isinstance(trials, list) or not isinstance(reviews, list):
        raise ValueError("Trials and reviews must be lists")
    index = {}
    for trial in trials:
        if not isinstance(trial, dict):
            raise ValueError("Trials must be objects")
        _text(trial.get("trial_id"), "trial_id")
        _json_safe(trial)
        if trial["trial_id"] in index:
            raise ValueError("Duplicate trial IDs")
        index[trial["trial_id"]] = trial
    accepted = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("Review must be an object")
        trial_id = review.get("trial_id")
        _text(trial_id, "review trial_id")
        if trial_id in accepted:
            raise ValueError("Duplicate or conflicting reviews for one trial")
        if trial_id not in index:
            raise ValueError("Review refers to an unknown trial")
        _validate_review(review, index[trial_id], rubric)
        accepted[trial_id] = review
    rubric_hash = fingerprint(rubric)
    copied = deepcopy(trials)
    for trial in copied:
        if trial["trial_id"] in accepted:
            review = accepted[trial["trial_id"]]
            trial.update(quality_score=review["score"],
                         quality_source=f"human-review:{rubric['rubric_id']}:{rubric_hash}",
                         quality_review_fingerprint=fingerprint(review))
    ordered = [deepcopy(accepted[key]) for key in sorted(accepted)]
    eligible = sum(_eligible(trial) for trial in trials)
    evidence = {
        "schema_version": 1, "record_type": "QualityReviewEvidence", "rubric": deepcopy(rubric),
        "rubric_sha256": rubric_hash, "reviews": ordered,
        "review_fingerprints": [fingerprint(review) for review in ordered],
        "coverage": {"retained_trials": len(trials), "eligible_trials": eligible,
                     "reviewed_trials": len(ordered), "unreviewed_eligible_trials": eligible - len(ordered)},
    }
    evidence["fingerprint"] = fingerprint(evidence)
    return copied, evidence
