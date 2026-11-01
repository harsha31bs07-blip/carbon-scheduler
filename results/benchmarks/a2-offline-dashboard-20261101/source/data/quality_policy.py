"""Explicit pilot review coverage requirements, separate from semantic scores."""

from copy import deepcopy
import math
from pathlib import Path

from data.quality_reviews import load_json


DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "config/quality/pilot-v1.json"


def validate_quality_policy(policy):
    expected = {"schema_version", "record_type", "policy_id", "minimum_reviewed_trials",
                "minimum_distinct_cases", "minimum_reviewed_fraction"}
    if not isinstance(policy, dict) or set(policy) != expected:
        raise ValueError("Invalid QualityCoveragePolicy fields")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1 or policy["record_type"] != "QualityCoveragePolicy":
        raise ValueError("Unsupported QualityCoveragePolicy")
    if not isinstance(policy["policy_id"], str) or not policy["policy_id"].strip():
        raise ValueError("Quality coverage policy requires an ID")
    for name in ("minimum_reviewed_trials", "minimum_distinct_cases"):
        if type(policy[name]) is not int or policy[name] < 1:
            raise ValueError("Quality coverage counts must be positive integers")
    fraction = policy["minimum_reviewed_fraction"]
    try:
        valid = type(fraction) in {int, float} and math.isfinite(fraction) and 0 < fraction <= 1
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Quality coverage fraction must be in (0, 1]")
    return deepcopy(policy)


def load_quality_policy(path=None):
    return validate_quality_policy(load_json(DEFAULT_POLICY_PATH if path is None else path))


def quality_coverage(policy, *, reviewed_trials, valid_trials, case_ids):
    """Retain observed scores while deciding if they can supply a prediction."""
    policy = validate_quality_policy(policy)
    if (type(reviewed_trials) is not int or type(valid_trials) is not int
            or not 0 <= reviewed_trials <= valid_trials or not isinstance(case_ids, list)
            or any(not isinstance(case, str) or not case.strip() for case in case_ids)
            or len(set(case_ids)) != len(case_ids) or len(case_ids) > reviewed_trials):
        raise ValueError("Invalid quality coverage observations")
    fraction = reviewed_trials / valid_trials if valid_trials else 0.
    reasons = []
    if reviewed_trials < policy["minimum_reviewed_trials"]:
        reasons.append("insufficient-reviewed-trials")
    if len(case_ids) < policy["minimum_distinct_cases"]:
        reasons.append("insufficient-distinct-cases")
    if fraction < policy["minimum_reviewed_fraction"]:
        reasons.append("insufficient-reviewed-fraction")
    return {"reviewed_trials": reviewed_trials, "valid_trials": valid_trials,
            "distinct_case_ids": sorted(case_ids), "reviewed_fraction": fraction,
            "eligible": not reasons, "reasons": reasons}
