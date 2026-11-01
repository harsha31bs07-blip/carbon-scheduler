"""Freeze aggregate profiles from completed calibration journals without inference."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from data.profiles import build_profiles
from data.quality_reviews import apply_reviews, load_json, load_rubric
from data.quality_policy import load_quality_policy
from workflow.scheduling_records import write_json


AGGREGATE_SCHEMA_VERSION = 2
IDENTITY_METHOD = "eligible-profile-content-v1"


def load_calibration_inputs(directories):
    """Validate completed journals before review or profile construction."""
    if not isinstance(directories, (tuple, list)) or not directories:
        raise ValueError("At least one calibration input is required")
    trials, inputs = [], []
    identities = set()
    for raw in directories:
        directory = Path(raw)
        manifest_path = directory / "manifest.json"
        manifest = load_json(manifest_path)
        if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
                or manifest["schema_version"] != 1 or manifest.get("record_type") != "CalibrationRun"
                or manifest.get("status") != "completed"
                or manifest.get("split") != "calibration"):
            raise ValueError("Only completed calibration runs may train frozen profiles")
        if not isinstance(manifest.get("run_id"), str) or not manifest["run_id"].strip():
            raise ValueError("Calibration input must have a run ID")
        # Parse once, strictly, before records enter a review or profile hash.
        records = []
        for path in directory.glob("*.json"):
            value = load_json(path)
            if isinstance(value, dict) and value.get("record_type") == "CalibrationTrial":
                records.append(value)
        records.sort(key=lambda item: (item.get("started_at", ""), item["trial_id"]))
        if (type(manifest.get("retained_trials")) is not int
                or len(records) != manifest["retained_trials"] or not records):
            raise ValueError("Retained trial count does not match the manifest")
        if any(trial.get("split") != "calibration" for trial in records):
            raise ValueError("Held-out trials cannot train aggregate profiles")
        # Validate typed records before their IDs enter sets or canonical hashes.
        build_profiles(records, calibration_id=manifest["run_id"])
        ids = [record["trial_id"] for record in records]
        if len(set(ids)) != len(ids) or identities.intersection(ids):
            raise ValueError("Calibration inputs contain duplicate trials")
        identities.update(ids)
        digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        inputs.append({"directory": str(directory), "run_id": manifest["run_id"],
                       "retained_trials": len(records), "trials_fingerprint": digest,
                       "manifest_fingerprint": hashlib.sha256(manifest_path.read_bytes()).hexdigest()})
        trials.extend(records)
    return trials, inputs


def load_review_files(paths):
    if not isinstance(paths, (tuple, list)):
        raise ValueError("Review files must be a list")
    reviews = []
    for path in paths:
        value = load_json(path)
        if not isinstance(value, list):
            raise ValueError("A review file must contain a JSON list of approved reviews")
        reviews.extend(value)
    return reviews


def freeze_profiles(directories, output, *, review_paths=None, rubric_path=None, quality_policy_path=None):
    output = Path(output)
    if output.exists():
        raise ValueError("Aggregate output must be a new directory")
    if rubric_path is not None and review_paths is None:
        raise ValueError("A custom rubric requires review inputs")
    trials, inputs = load_calibration_inputs(directories)
    evidence = None
    quality_policy = load_quality_policy(quality_policy_path) if review_paths is not None or quality_policy_path is not None else None
    if review_paths is not None:
        trials, evidence = apply_reviews(trials, load_review_files(review_paths), rubric=load_rubric(rubric_path))
    # Full raw journals stay auditable in inputs, but excluded warm-ups and
    # incidental journal metadata cannot change the fitted-profile identity.
    # Canonical profiles also remove dependence on directory ordering/grouping.
    trials.sort(key=lambda record: record["trial_id"])
    identity = build_profiles(trials, calibration_id=IDENTITY_METHOD, quality_policy=quality_policy)["fingerprint"]
    profiles = build_profiles(trials, calibration_id=identity, quality_policy=quality_policy)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "profiles.json", profiles)
    if evidence is not None:
        write_json(output / "quality_reviews.json", evidence)
    inputs.sort(key=lambda item: (item["run_id"], item["trials_fingerprint"], item["directory"]))
    write_json(output / "manifest.json", {"schema_version": AGGREGATE_SCHEMA_VERSION,
        "record_type": "FrozenCalibrationAggregate", "identity_method": IDENTITY_METHOD,
        "calibration_id": identity, "inputs": inputs, "summary": profiles["summary"],
        "quality_status": "only-explicitly-reviewed-trials-can-supply-quality",
        **({"quality_policy": quality_policy,
            "quality_eligibility": {"eligible_profiles": sum(
                profile["metrics"]["accuracy"]["coverage"]["eligible"]
                and profile["metrics"]["accuracy"]["comparable"] for profile in profiles["profiles"]),
                "profile_count": len(profiles["profiles"])}} if quality_policy is not None else {}),
        **({"quality_review_evidence_sha256": hashlib.sha256(
            (output / "quality_reviews.json").read_bytes()).hexdigest(),
            "quality_review_coverage": evidence["coverage"]} if evidence is not None else {})})
    return profiles


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviews", nargs="+", type=Path,
                        help="JSON lists of approved human reviews; raw journals remain unchanged")
    parser.add_argument("--rubric", type=Path, help="Versioned rubric used by those reviews")
    parser.add_argument("--quality-policy", type=Path,
                        help="Review coverage requirements; reviewed freezes default to config/quality/pilot-v1.json")
    args = parser.parse_args(argv)
    try:
        profiles = freeze_profiles(args.input, args.output, review_paths=args.reviews, rubric_path=args.rubric,
                                   quality_policy_path=args.quality_policy)
        print(json.dumps({"calibration_id": profiles["calibration_id"], "summary": profiles["summary"]}, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError):
        print(json.dumps({"error_code": "invalid-calibration-aggregate-input"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
