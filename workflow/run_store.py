"""Validate run artifacts and pair explicitly related experiments.

This module has no Streamlit, provider, or network dependencies. Legacy records
remain browsable, but missing configuration is never inferred for comparisons.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from workflow.dag import STEP_IDS


POLICIES = {"baseline", "eco", "balanced", "speed", "custom"}
CONTEXT_FIELDS = (
    "comparison_id", "workload_id", "provider_mode", "reference_utc_time",
    "calibration_id", "execution_mode",
)
RUN_FIELDS = ("schema_version", "run_id", "policy", "custom_weights") + CONTEXT_FIELDS
METRICS = ("cost", "energy", "carbon", "actual_latency_ms", "accuracy_estimate")


def _valid_number(value: object, *, maximum: float | None = None) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value >= 0
        and (maximum is None or value <= maximum)
    )


def aggregate_run_totals(records: list[dict]) -> dict:
    """Aggregate valid metrics; absent or invalid measurements are not zeroes."""
    if not records or any(
        not _valid_number(record.get(key), maximum=1 if key == "accuracy_estimate" else None)
        for record in records for key in METRICS
    ):
        raise ValueError("Run metrics must be present, finite, and non-negative")
    if any(not _valid_number(record.get("execution_latency_ms", record["actual_latency_ms"])) for record in records):
        raise ValueError("Execution latency must be finite and non-negative")
    totals = {
        "cost": sum(record["cost"] for record in records),
        "energy": sum(record["energy"] for record in records),
        "carbon": sum(record["carbon"] for record in records),
        "latency": sum(record.get("execution_latency_ms", record["actual_latency_ms"]) for record in records),
        "accuracy": sum(record["accuracy_estimate"] for record in records) / len(records),
    }
    if not all(math.isfinite(value) for value in totals.values()):
        raise ValueError("Run totals overflowed")
    return totals


def load_run(file_path: Path | str) -> dict | None:
    """Return one complete, consistent run, or None for unusable artifacts.

    Schema 2 additionally needs a matching completed manifest. An interrupted
    append, malformed JSON line, or duplicate step invalidates the whole run.
    """
    file_path = Path(file_path)
    try:
        records = [json.loads(line) for line in file_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(records) != len(STEP_IDS) or not all(isinstance(record, dict) for record in records):
            return None
        steps = [record.get("step_id") for record in records]
        if any(not isinstance(step, str) for step in steps) or set(steps) != set(STEP_IDS):
            return None
        first = records[0]
        if any(any(record.get(key) != first.get(key) for key in RUN_FIELDS) for record in records):
            return None
        version = first.get("schema_version", 1)
        if type(version) is not int or version not in (1, 2):
            return None
        run_id = first.get("run_id") or file_path.stem
        if run_id != file_path.stem:
            return None
        if any(not isinstance(record.get(key), str) or not record[key].strip()
               for record in records for key in ("model", "location")):
            return None
        if any(not _valid_number(record.get("time_offset", 0)) for record in records):
            return None
        totals = aggregate_run_totals(records)
        if version == 2:
            if first.get("policy") not in POLICIES:
                return None
            if any(key not in record for record in records for key in RUN_FIELDS):
                return None
            if any(record.get("status") != "completed" for record in records):
                return None
            if first.get("execution_mode") not in {"live", "replay"}:
                return None
            manifest_path = file_path.with_suffix(".manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("status") != "completed" or type(manifest.get("schema_version")) is not int:
                return None
            if any(key not in manifest or manifest[key] != first[key] for key in RUN_FIELDS):
                return None
            if manifest.get("expected_steps") != STEP_IDS:
                return None
        records.sort(key=lambda record: STEP_IDS.index(record["step_id"]))
        return {
            "run_id": run_id,
            "policy": first.get("policy") or "unknown",
            "schema_version": version,
            "records": records,
            "totals": totals,
            "mtime": file_path.stat().st_mtime,
            **{key: first.get(key) for key in CONTEXT_FIELDS},
        }
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
        return None


def load_runs(results_dir: Path | str) -> list[dict]:
    """Read complete runs, latest first; malformed files never crash the UI."""
    return sorted(
        (run for path in Path(results_dir).glob("*.jsonl") if (run := load_run(path)) is not None),
        key=lambda run: (run["mtime"], run["run_id"]), reverse=True,
    )


def select_comparison(runs: list[dict], scheduled_run_id: str | None = None) -> dict | None:
    """Select an unambiguous baseline/policy pair sharing explicit context.

    If a run is selected in the UI, only its pair is eligible. Modification
    times choose among valid pairs, never between unrelated individual runs.
    """
    groups: dict[str, list[dict]] = {}
    for run in runs:
        comparison_id = run.get("comparison_id")
        if run.get("schema_version") == 2 and isinstance(comparison_id, str) and comparison_id.strip():
            groups.setdefault(comparison_id, []).append(run)
    pairs = []
    for comparison_id, members in groups.items():
        if len(members) != 2:
            continue
        baseline = [run for run in members if run["policy"] == "baseline"]
        scheduled = [run for run in members if run["policy"] in POLICIES - {"baseline"}]
        if len(baseline) != 1 or len(scheduled) != 1:
            continue
        base, policy = baseline[0], scheduled[0]
        if scheduled_run_id is not None and policy["run_id"] != scheduled_run_id:
            continue
        if any(not isinstance(base.get(key), str) or not base[key].strip()
               or base[key] != policy.get(key) for key in CONTEXT_FIELDS):
            continue
        pairs.append({"comparison_id": comparison_id, "baseline": base, "scheduled": policy})
    return max(pairs, key=lambda pair: max(pair["baseline"]["mtime"], pair["scheduled"]["mtime"])) if pairs else None


def provenance_label(records: list[dict], metric: str) -> str:
    """Preserve mixed and unknown provenance instead of claiming measurement."""
    labels = [
        record.get("provenance", {}).get(metric, "unknown")
        if isinstance(record.get("provenance"), dict) else "unknown"
        for record in records
    ]
    if not all(isinstance(label, str) for label in labels):
        return "Unknown"
    unique_labels = set(labels)
    return next(iter(unique_labels)).replace("_", " ").capitalize() if len(unique_labels) == 1 else "Mixed sources"
