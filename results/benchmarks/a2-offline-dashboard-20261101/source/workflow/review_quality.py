"""Export human review packets and validate approvals without inference."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from data.quality_reviews import apply_reviews, fingerprint, load_json, load_rubric, review_template
from shared.run_records import write_json
from workflow.profile_report import load_calibration_inputs, load_review_files
from workflow.workload import KnowledgeBase


def _recorded_context(directory):
    """Use the retained experiment's fixtures, never today's mutable workload."""
    directory = Path(directory)
    snapshot_path = directory / "source_snapshot.json"
    if not snapshot_path.exists():
        return {"status": "unavailable", "reason": "no-retained-source-snapshot"}
    snapshot = load_json(snapshot_path)
    manifest = load_json(directory / "manifest.json")
    if (not isinstance(snapshot, dict) or type(snapshot.get("schema_version")) is not int
            or snapshot["schema_version"] != 1 or snapshot.get("record_type") != "CalibrationSourceSnapshot"
            or not isinstance(snapshot.get("files"), dict)):
        raise ValueError("Invalid retained source snapshot")
    expected_snapshot = manifest.get("source_snapshot_fingerprint")
    method = "canonical-json-v1"
    if snapshot.get("fingerprint") != fingerprint(snapshot["files"]):
        # The first retained pilot's external source capture used json.dumps'
        # default separators and a scope field, before capture moved into the
        # runner. Recognize that specific older envelope and retain its method.
        legacy_hash = hashlib.sha256(json.dumps(snapshot["files"], sort_keys=True,
            allow_nan=False).encode("utf-8")).hexdigest()
        if (expected_snapshot is not None or not isinstance(snapshot.get("scope"), str)
                or not snapshot["scope"].strip() or snapshot.get("fingerprint") != legacy_hash):
            raise ValueError("Invalid retained source snapshot fingerprint")
        method = "legacy-json-default-separators-v1"
    if expected_snapshot is not None and expected_snapshot != snapshot["fingerprint"]:
        raise ValueError("Source snapshot differs from the experiment manifest")
    context = {"status": "retained", "source_snapshot_fingerprint": snapshot["fingerprint"],
               "snapshot_hash_method": method}
    for key, name in (("cases", "data/workloads/support_tickets.v1.json"),
                      ("knowledge_base", "data/workloads/support_kb.v1.json")):
        path = directory / "source" / name
        if name not in snapshot["files"] or hashlib.sha256(path.read_bytes()).hexdigest() != snapshot["files"][name]:
            raise ValueError("Retained review context differs from the experiment snapshot")
        context[key] = load_json(path)
    cases = context["cases"]
    if not isinstance(cases, dict) or not isinstance(cases.get("tickets"), list):
        raise ValueError("Retained cases must contain versioned tickets")
    selected = [case for case in cases["tickets"] if isinstance(case, dict) and case.get("split") == "calibration"]
    if manifest.get("cases_fingerprint") is not None and manifest["cases_fingerprint"] != fingerprint(selected):
        raise ValueError("Retained cases differ from the experiment manifest")
    if manifest.get("kb_fingerprint") is not None:
        kb = KnowledgeBase.load(directory / "source/data/workloads/support_kb.v1.json")
        if kb.fingerprint() != manifest["kb_fingerprint"]:
            raise ValueError("Retained KB differs from the experiment manifest")
    context["manifest_binding"] = ("source-snapshot" if expected_snapshot is not None
        else "fixture-fingerprints" if manifest.get("cases_fingerprint") is not None
            and manifest.get("kb_fingerprint") is not None else "unavailable")
    return context


def export_reviews(directories, output, *, rubric_path=None):
    """Create a new packet plus blank templates; never supply human judgments."""
    output = Path(output)
    if output.exists():
        raise ValueError("Review packet output must be a new directory")
    trials, inputs = load_calibration_inputs(directories)
    rubric = load_rubric(rubric_path)
    contexts = {str(Path(directory)): _recorded_context(directory) for directory in directories}
    templates, items, excluded = [], [], []
    for trial in sorted(trials, key=lambda item: item["trial_id"]):
        reasons = []
        if trial["warmup"]:
            reasons.append("warmup")
        if trial["status"] != "completed":
            reasons.append("not-completed")
        if not trial["output_valid"] or trial["completion_reason"] != "stop":
            reasons.append("not-valid-natural-completion")
        if reasons:
            excluded.append({"trial_id": trial["trial_id"], "reasons": reasons})
            continue
        template = review_template(trial, rubric)
        templates.append(template)
        items.append({"trial_id": trial["trial_id"], "trial_sha256": fingerprint(trial),
                      "trial": trial})
    packet = {"schema_version": 1, "record_type": "QualityReviewPacket", "rubric": rubric,
              "rubric_sha256": fingerprint(rubric), "inputs": inputs, "recorded_contexts": contexts,
              "items": items, "excluded": excluded,
              "instructions": "Read the retained prompt, output, case and KB. Complete only reviews you personally judge. "
                  "Save a separate JSON list containing approved human reviews; omit pending entries. "
                  "Scores are the arithmetic mean of criterion scores. Do not edit journal evidence or its hashes."}
    # Everything is validated before creating the output directory. The manifest
    # is written last so an interrupted packet is visibly incomplete.
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "packet.json", packet)
    write_json(output / "reviews.pending.json", templates)
    manifest = {"schema_version": 1, "record_type": "QualityReviewPacketManifest",
                "rubric_sha256": fingerprint(rubric), "retained_trials": len(trials),
                "reviewable_trials": len(items), "excluded_trials": len(excluded),
                "files": {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                          for name in ("packet.json", "reviews.pending.json")}}
    write_json(output / "manifest.json", manifest)
    return manifest


def validate_reviews(directories, review_paths, *, rubric_path=None):
    trials, _ = load_calibration_inputs(directories)
    _, evidence = apply_reviews(trials, load_review_files(review_paths), rubric=load_rubric(rubric_path))
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Write prompts, retained fixtures and blank review templates")
    validate = commands.add_parser("validate", help="Check approvals against exact original trials")
    for command in (export, validate):
        command.add_argument("--input", nargs="+", type=Path, required=True)
        command.add_argument("--rubric", type=Path)
    export.add_argument("--output", type=Path, required=True)
    validate.add_argument("--reviews", nargs="+", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            report = export_reviews(args.input, args.output, rubric_path=args.rubric)
        else:
            evidence = validate_reviews(args.input, args.reviews, rubric_path=args.rubric)
            report = {"rubric_sha256": evidence["rubric_sha256"], "coverage": evidence["coverage"]}
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError):
        print(json.dumps({"error_code": "invalid-quality-review-input"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
