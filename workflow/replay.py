"""Export and read immutable, explicitly selected live-run replay bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil

from shared.run_records import validate_id, write_json
from shared.schema import Candidate
from workflow.run_store import load_run, select_comparison


class ReplayError(RuntimeError):
    """The selected evidence cannot be replayed; never substitute a live call."""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle_file(directory: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name:
        raise ReplayError("Replay file must be a filename inside the bundle")
    path = directory / name
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ReplayError("Replay file points outside the bundle")
    return path


def _validate_evidence(run: dict) -> None:
    if run.get("schema_version") != 2 or run.get("execution_mode") != "live":
        raise ReplayError("Replay requires complete schema-2 live evidence; legacy/replay runs are ineligible")
    for record in run["records"]:
        if record.get("replay_used") or not isinstance(record.get("output_text"), str):
            raise ReplayError("Replay source must contain original live outputs")
        if not isinstance(record.get("timestamp_utc"), str):
            raise ReplayError("Replay source lacks its measurement timestamp")
        for field in ("input_tokens", "output_tokens", "execution_latency_ms"):
            value = record.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ReplayError(f"Invalid replay measurement: {field}")
        for field in ("scheduled_candidate", "executed_candidate"):
            try:
                candidate = Candidate.from_dict(record[field])
            except (KeyError, TypeError) as exc:
                raise ReplayError(f"Replay source lacks a valid {field}") from exc
            if candidate.step_id != record["step_id"] or candidate.time_offset_hr != 0:
                raise ReplayError("Replay source has incompatible step/deferral metadata")
        if record["executed_candidate"]["model"] != record["model"]:
            raise ReplayError("Replay model does not match executed candidate")


def load_replay_run(directory: Path, policy: str, custom_weights: dict | None,
                    expected_workload: str) -> dict:
    directory = Path(directory)
    try:
        index = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if index.get("schema_version") != 1:
            raise ReplayError("Unsupported replay bundle version")
        entry = index["runs"][policy]
        path = _bundle_file(directory, entry["file"])
        manifest_path = _bundle_file(directory, entry["manifest"])
        if manifest_path != path.with_suffix(".manifest.json"):
            raise ReplayError("Replay manifest does not match its run")
        if _digest(path) != entry["sha256"] or _digest(manifest_path) != entry["manifest_sha256"]:
            raise ReplayError("Replay evidence checksum does not match the bundle")
        run = load_run(path)
        if run is None:
            raise ReplayError("Replay run is malformed, incomplete, or has inconsistent metadata")
        _validate_evidence(run)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if run["policy"] != policy or manifest["custom_weights"] != custom_weights:
            raise ReplayError("Replay policy/weights differ from the selected recording")
        if run["workload_id"] != expected_workload:
            raise ReplayError("Replay workload differs from the current prompt version")
        return {"manifest": manifest, "records": {r["step_id"]: r for r in run["records"]}}
    except ReplayError:
        raise
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ReplayError(
            f"No valid '{policy}' replay in {directory}. Export a completed live comparison first."
        ) from exc


def export_comparison(results_dir: Path, baseline_id: str, scheduled_id: str,
                      output: Path) -> Path:
    """Copy one complete compatible pair; publish its checksummed index last."""
    validate_id(baseline_id)
    validate_id(scheduled_id)
    results_dir, output = Path(results_dir), Path(output)
    runs = [load_run(results_dir / f"{run_id}.jsonl") for run_id in (baseline_id, scheduled_id)]
    if any(run is None for run in runs) or select_comparison(runs, scheduled_id) is None:
        raise ReplayError("Export requires a complete, matched baseline/scheduled pair")
    for run in runs:
        _validate_evidence(run)
    # A bundle is a reviewed snapshot, never silently replaced by a new run.
    output.mkdir(parents=True, exist_ok=False)
    index = {"schema_version": 1, "runs": {}}
    for run in runs:
        path = results_dir / f"{run['run_id']}.jsonl"
        manifest_path = path.with_suffix(".manifest.json")
        target, target_manifest = output / path.name, output / manifest_path.name
        shutil.copyfile(path, target)
        shutil.copyfile(manifest_path, target_manifest)
        index["runs"][run["policy"]] = {
            "file": target.name, "manifest": target_manifest.name,
            "sha256": _digest(target), "manifest_sha256": _digest(target_manifest),
        }
    write_json(output / "manifest.json", index)
    return output


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Completed live baseline run ID")
    parser.add_argument("--scheduled", required=True, help="Its matching scheduled run ID")
    parser.add_argument("--results-dir", type=Path, default=root / "results" / "runs")
    parser.add_argument("--output", type=Path, required=True, help="New directory for the fixture bundle")
    args = parser.parse_args()
    print(export_comparison(args.results_dir, args.baseline, args.scheduled, args.output))


if __name__ == "__main__":
    main()
