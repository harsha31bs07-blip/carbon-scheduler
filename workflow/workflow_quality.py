"""Human output review bound to frozen workflow evidence; no model judgments."""

from copy import deepcopy
from datetime import datetime, timezone
import math

from data.quality_reviews import fingerprint, load_rubric, _validate_rubric
from workflow.workload import STEP_ORDER


def default_review_config():
    return {"rubric": load_rubric(), "policy": {"schema_version": 1,
        "record_type": "WorkflowQualityPolicy", "policy_id": "workflow-quality-pilot.v1",
        "aggregation": "minimum-step-mean.v1", "minimum_score": .8, "minimum_criterion_score": .5}}


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("Unsupported workflow quality evidence fields")


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Human review requires nonempty attribution and rationale")


def _score(value):
    if type(value) not in {int, float} or not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError("Quality scores must be finite ratios")


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Review time must be an aware ISO timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
    except ValueError:
        raise ValueError("Review time must be an aware ISO timestamp") from None
    return result.astimezone(timezone.utc)


def validate_review_config(value):
    _shape(value, {"rubric", "policy"})
    fingerprint(value)
    _validate_rubric(value["rubric"])
    policy = value["policy"]
    _shape(policy, {"schema_version", "record_type", "policy_id", "aggregation", "minimum_score", "minimum_criterion_score"})
    if (type(policy["schema_version"]) is not int or policy["schema_version"] != 1
            or policy["record_type"] != "WorkflowQualityPolicy" or policy["aggregation"] != "minimum-step-mean.v1"):
        raise ValueError("Unsupported workflow quality policy")
    _text(policy["policy_id"])
    _score(policy["minimum_score"])
    _score(policy["minimum_criterion_score"])
    return deepcopy(value)


def _binding(protocol, trial, snapshot):
    from workflow.evaluation_protocol import validate_trial_snapshot
    from workflow.queue_replay import _snapshot_dict, _decode_snapshot
    if "output_quality" not in protocol:
        raise ValueError("This protocol did not freeze an output-review policy")
    if trial.get("state") != "finished":
        raise ValueError("Only finished trials can receive output review")
    validate_review_config(protocol["output_quality"])
    snapshot = _decode_snapshot(_snapshot_dict(snapshot))
    validate_trial_snapshot(protocol, trial, snapshot)
    if snapshot.status != "completed" or set(snapshot.completed_outputs()) != set(STEP_ORDER):
        raise ValueError("End-to-end review requires all accepted workflow outputs")
    for job in snapshot.jobs:
        if not any(attempt.planned_candidate == job.candidate and attempt.retry_index == job.retry_index
                   and attempt.output == job.output and attempt.status == "completed" and attempt.completion_reason == "stop"
                   for attempt in snapshot.attempts):
            raise ValueError("Review requires accepted natural completions")
    return {"schema_version": 1, "record_type": "WorkflowQualityReview",
        "protocol_fingerprint": protocol["fingerprint"], "trial_id": trial["trial_id"],
        "workflow_id": snapshot.request.workflow_id, "workflow_sha256": fingerprint(_snapshot_dict(snapshot)),
        "outputs_sha256": fingerprint(snapshot.completed_outputs()),
        "rubric_sha256": fingerprint(protocol["output_quality"]["rubric"])}


def review_template(protocol, trial, snapshot):
    return {**_binding(protocol, trial, snapshot), "status": "pending", "review_method": "human-rubric",
        "reviewer": None, "reviewed_at": None, "steps": {step: {"criteria": {
            criterion["id"]: {"score": None, "rationale": ""}
            for criterion in protocol["output_quality"]["rubric"]["steps"][step]}, "score": None}
            for step in STEP_ORDER}, "score": None, "rationale": ""}


def validate_review(review, protocol, trial, snapshot, *, as_of):
    binding = _binding(protocol, trial, snapshot)
    _shape(review, set(binding) | {"status", "review_method", "reviewer", "reviewed_at", "steps", "score", "rationale"})
    fingerprint(review)
    if any(review[key] != value for key, value in binding.items()):
        raise ValueError("Review differs from exact protocol/workflow/output/rubric evidence")
    if review["status"] != "approved" or review["review_method"] != "human-rubric":
        raise ValueError("Only explicitly approved human judgments can supply quality")
    _text(review["reviewer"])
    _text(review["rationale"])
    reviewed, now = _time(review["reviewed_at"]), _time(as_of)
    facts = [snapshot.request.arrived_at, *(job.finished_at for job in snapshot.jobs)]
    facts.extend(_time(record["accounted_at"]) for record in snapshot.accounting)
    if not max(facts) <= reviewed <= now:
        raise ValueError("Review must follow retained completion and precede validation")
    _shape(review["steps"], STEP_ORDER)
    means = []
    for step in STEP_ORDER:
        value = review["steps"][step]
        _shape(value, {"criteria", "score"})
        ids = {item["id"] for item in protocol["output_quality"]["rubric"]["steps"][step]}
        _shape(value["criteria"], ids)
        scores = []
        for criterion in value["criteria"].values():
            _shape(criterion, {"score", "rationale"})
            _score(criterion["score"])
            _text(criterion["rationale"])
            scores.append(criterion["score"])
        mean = math.fsum(scores) / len(scores)
        _score(value["score"])
        if not math.isclose(mean, value["score"], rel_tol=0, abs_tol=1e-12):
            raise ValueError("Step score differs from human criterion mean")
        means.append(mean)
    _score(review["score"])
    if not math.isclose(min(means), review["score"], rel_tol=0, abs_tol=1e-12):
        raise ValueError("Workflow score must be the minimum step mean")
    return deepcopy(review)


def review_passes(review, config):
    policy = config["policy"]
    return (review["score"] >= policy["minimum_score"] and all(
        criterion["score"] >= policy["minimum_criterion_score"]
        for step in review["steps"].values() for criterion in step["criteria"].values()))


def make_review_set(protocol, trials, reviews, *, as_of, existing=None):
    from workflow.evaluation_protocol import validate_protocol
    protocol = validate_protocol(protocol)
    if "output_quality" not in protocol:
        raise ValueError("This protocol did not freeze an output-review policy")
    if _time(as_of) < _time(protocol["created_at"]):
        raise ValueError("Review validation cannot precede its frozen protocol")
    if not isinstance(reviews, list):
        raise ValueError("Approved workflow reviews must be a list")
    indexed = {item["entry"]["trial_id"]: item for item in trials}
    merged = {}
    if existing is not None:
        validate_review_set(existing, protocol, trials)
        if _time(as_of) < _time(existing["validated_at"]):
            raise ValueError("Review revisions cannot move backwards")
        merged = {item["trial_id"]: item for item in existing["reviews"]}
    seen = set()
    for review in reviews:
        if not isinstance(review, dict) or not isinstance(review.get("trial_id"), str):
            raise ValueError("Review requires a trial identity")
        identity = review["trial_id"]
        if identity in seen or identity not in indexed:
            raise ValueError("Duplicate or unknown reviewed trial")
        seen.add(identity)
        wrapped = indexed[identity]
        if wrapped["entry"]["state"] != "finished" or wrapped["snapshot"] is None:
            raise ValueError("Unfinished trials cannot receive output judgments")
        validated = validate_review(review, protocol, wrapped["entry"], wrapped["snapshot"], as_of=as_of)
        if identity in merged and merged[identity] != validated:
            raise ValueError("Approved human judgments cannot be overwritten")
        merged[identity] = validated
    value = {"schema_version": 1, "record_type": "WorkflowQualityReviewSet",
        "protocol_fingerprint": protocol["fingerprint"], "validated_at": as_of,
        "reviews": [merged[item["entry"]["trial_id"]] for item in trials if item["entry"]["trial_id"] in merged]}
    _time(as_of)
    value["fingerprint"] = fingerprint(value)
    return value


def validate_review_set(value, protocol, trials):
    _shape(value, {"schema_version", "record_type", "protocol_fingerprint", "validated_at", "reviews", "fingerprint"})
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["record_type"] != "WorkflowQualityReviewSet" or value["protocol_fingerprint"] != protocol["fingerprint"]):
        raise ValueError("Unsupported workflow review set")
    expected = make_review_set(protocol, trials, value["reviews"], as_of=value["validated_at"])
    if value != expected:
        raise ValueError("Workflow review set differs from validated evidence")
    return deepcopy(value)
