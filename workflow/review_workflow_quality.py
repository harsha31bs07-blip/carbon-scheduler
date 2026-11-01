"""Export blank workflow review packets and import attributable human approvals."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from data.quality_reviews import load_json
from workflow.workflow_quality import review_template, make_review_set


def review_inputs(directory):
    from workflow.policy_comparison import _read_evaluation, _json, load_comparison
    from workflow.queue_replay import _decode_snapshot
    directory = Path(directory)
    if (directory / "manifest.json").exists():
        load_comparison(directory)
    _, files = _read_evaluation(directory)
    protocol, journal = _json(files["protocol.json"]), _json(files["evaluation.json"])
    trials = [{"entry": entry, "snapshot": _decode_snapshot(_json(files[entry["bundle"] + "/workflow.json"]))
               if entry["state"] == "finished" else None} for entry in journal["trials"]]
    return protocol, trials


def export_reviews(directory, output):
    from workflow.queue_replay import _snapshot_dict
    from workflow.evaluate_policies import _write
    protocol, trials = review_inputs(directory)
    if "output_quality" not in protocol:
        raise ValueError("A new protocol must freeze its review policy before execution")
    items, templates, excluded = [], [], []
    for wrapped in trials:
        entry, snapshot = wrapped["entry"], wrapped["snapshot"]
        if snapshot is None or snapshot.status != "completed":
            excluded.append({"trial_id": entry["trial_id"], "reason": "no-completed-workflow"})
            continue
        try:
            template = review_template(protocol, entry, snapshot)
        except ValueError:
            excluded.append({"trial_id": entry["trial_id"], "reason": "natural-completion-evidence-required"})
            continue
        templates.append(template)
        items.append({"entry": entry, "workflow": _snapshot_dict(snapshot)})
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    packet = {"schema_version": 1, "record_type": "WorkflowQualityReviewPacket", "protocol": protocol,
        "items": items, "excluded": excluded,
        "instructions": "Judge the actual ticket, KB, prompts and all four accepted outputs. Fill every criterion score and rationale; "
            "set step scores to criterion means and workflow score to their minimum. Approve only your own human judgments. "
            "Save approvals as a separate JSON list. Hashes bind content, not reviewer identity."}
    _write(output / "packet.json", packet)
    _write(output / "reviews.pending.json", templates)
    manifest = {"schema_version": 1, "record_type": "WorkflowQualityReviewPacketManifest",
        "protocol_fingerprint": protocol["fingerprint"], "reviewable_trials": len(items),
        "excluded_trials": len(excluded), "files": {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in ("packet.json", "reviews.pending.json")}}
    _write(output / "manifest.json", manifest)
    return manifest


def import_reviews(directory, reviews, *, as_of=None):
    from workflow.evaluate_policies import _runner_lock, _write
    directory = Path(directory)
    if (directory / "manifest.json").exists():
        raise ValueError("Portable comparisons are immutable; import into the original evaluation")
    with _runner_lock(directory):
        protocol, trials = review_inputs(directory)
        path = directory / "quality_reviews.json"
        existing = load_json(path) if path.exists() else None
        stamp = as_of or datetime.now(timezone.utc).isoformat()
        value = make_review_set(protocol, trials, reviews, as_of=stamp, existing=existing)
        if existing is not None and value["reviews"] == existing["reviews"]:
            return existing
        if existing is not None:
            history = directory / "quality-review-history"
            if history.is_symlink():
                raise ValueError("Review history must stay inside the evaluation directory")
            history.mkdir(exist_ok=True)
            old = history / (existing["fingerprint"] + ".json")
            if old.is_symlink():
                raise ValueError("Review history cannot follow symlinks")
            if old.exists() and load_json(old) != existing:
                raise ValueError("Review revision history conflicts with retained evidence")
            if not old.exists():
                _write(old, existing)
        _write(path, value)
        return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--evaluation", required=True)
    export.add_argument("--output", required=True)
    for name in ("validate", "import"):
        command = commands.add_parser(name)
        command.add_argument("--evaluation", required=True)
        command.add_argument("--reviews", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_reviews(args.evaluation, args.output)
        else:
            reviews = load_json(args.reviews)
            if args.command == "import":
                result = import_reviews(args.evaluation, reviews)
            else:
                protocol, trials = review_inputs(args.evaluation)
                result = make_review_set(protocol, trials, reviews, as_of=datetime.now(timezone.utc).isoformat())
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"error_code": "invalid-workflow-quality-review"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
