"""Offline matched policy evidence, with failures and incomplete metrics intact.

These are descriptive comparisons of serial, randomized trial blocks. Matching
does not make nonsimultaneous observations causal counterfactuals, and unknown
end-to-end quality never becomes a quality-preservation or release claim.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import statistics

from data.quality_reviews import fingerprint
from workflow.accounting_report import execution_report
from workflow.evaluation_protocol import schedule_trials, validate_protocol, validate_trial_snapshot
from workflow.queue_replay import _decode_snapshot, _snapshot_dict


_JOURNAL_FIELDS = {"schema_version", "record_type", "protocol_fingerprint", "status",
                   "created_at", "updated_at", "trials", "fingerprint"}
_IDENTITY_FIELDS = {"trial_id", "case_id", "repeat", "arm_id", "workflow_id"}
_ENTRY_FIELDS = _IDENTITY_FIELDS | {"state", "bundle", "started_at", "finished_at", "stop_reason"}
_MANIFEST_FIELDS = {"schema_version", "record_type", "protocol_fingerprint", "files", "fingerprint"}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_METRICS = ("response_seconds", "completion_seconds", "cost", "energy", "carbon")
_MAX_FILE_BYTES = 64 * 1024 * 1024


def _malformed(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError("Malformed matched policy comparison evidence") from exc
    return checked


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Evaluation timestamps must be timezone-aware ISO strings")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid evaluation timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Evaluation timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate evaluation JSON field")
            result[key] = value
        return result
    def invalid(_value):
        raise ValueError("Evaluation JSON must contain finite values")
    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Malformed evaluation JSON") from None
    fingerprint(value)  # Also rejects exponent overflow and unsupported JSON values.
    return value


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("Evidence paths must be safe relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Evidence paths cannot traverse outside their bundle")
    if str(path) != value:
        raise ValueError("Evidence paths must be canonical")
    return path


def _safe_file(root, name):
    path = root.joinpath(*_relative(name).parts)
    if root.is_symlink() or any(part.is_symlink() for part in (path, *path.parents) if part != root.parent):
        raise ValueError("Evidence cannot follow symlinks")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Required evidence file is missing or outside its bundle")
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("Evidence file exceeds the supported size bound")
    return path


def _sealed(value, fields, record_type):
    if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["record_type"] != record_type
            or not isinstance(value["fingerprint"], str) or not _SHA.fullmatch(value["fingerprint"])):
        raise ValueError("Unsupported matched evaluation evidence schema")
    if value["fingerprint"] != fingerprint({key: item for key, item in value.items() if key != "fingerprint"}):
        raise ValueError("Matched evaluation evidence fingerprint mismatch")


def _entry(value, expected):
    if (not isinstance(value, dict) or set(value) != _ENTRY_FIELDS
            or type(value["repeat"]) is not int
            or {key: value[key] for key in _IDENTITY_FIELDS} != expected
            or value["state"] not in {"pending", "submitted", "finished"}):
        raise ValueError("Trial entry differs from its frozen randomized schedule")
    started = _time(value["started_at"]) if value["started_at"] is not None else None
    finished = _time(value["finished_at"]) if value["finished_at"] is not None else None
    reason = value["stop_reason"]
    if reason is not None and (not isinstance(reason, str) or not _CODE.fullmatch(reason)):
        raise ValueError("Trial stop reason must be a safe diagnostic code")
    if value["state"] == "pending":
        if any(value[key] is not None for key in ("bundle", "started_at", "finished_at", "stop_reason")):
            raise ValueError("Pending trials cannot contain execution facts")
    elif value["state"] == "submitted":
        if started is None or any(value[key] is not None for key in ("bundle", "finished_at", "stop_reason")):
            raise ValueError("Submitted trials cannot contain a finished bundle")
    elif (started is None or finished is None or finished < started
          or value["bundle"] != "bundles/" + expected["trial_id"]):
        raise ValueError("Finished trials require their exact bundle and ordered timestamps")
    return started, finished


def _journal(value, protocol):
    _sealed(value, _JOURNAL_FIELDS, "MatchedPolicyEvaluation")
    if value["protocol_fingerprint"] != protocol["fingerprint"]:
        raise ValueError("Journal differs from its frozen protocol")
    if value["status"] not in {"prepared", "running", "completed", "interrupted"}:
        raise ValueError("Unsupported evaluation journal status")
    created, updated = _time(value["created_at"]), _time(value["updated_at"])
    if not _time(protocol["created_at"]) <= created <= updated:
        raise ValueError("Journal times precede protocol freeze or are out of order")
    expected = schedule_trials(protocol)
    if not isinstance(value["trials"], list) or len(value["trials"]) != len(expected):
        raise ValueError("Journal must retain every scheduled trial exactly once")
    previous_finish, unfinished_seen = created, False
    for entry, identity in zip(value["trials"], expected):
        started, finished = _entry(entry, identity)
        if started is not None:
            if unfinished_seen or not previous_finish <= started <= updated:
                raise ValueError("Trial execution contradicts the declared serial schedule")
            if finished is not None:
                if finished > updated:
                    raise ValueError("Trial finish exceeds the journal update")
                previous_finish = finished
        if entry["state"] != "finished":
            unfinished_seen = True
    states = {entry["state"] for entry in value["trials"]}
    if value["status"] == "prepared" and states != {"pending"}:
        raise ValueError("Prepared evaluation cannot contain started trials")
    if value["status"] == "completed" and states != {"finished"}:
        raise ValueError("Completed evaluation cannot omit unfinished trials")
    return deepcopy(value)


@_malformed
def validate_evaluation_journal(value, protocol):
    """Validate serial schedule/chronology without reading a queue or provider."""
    return _journal(value, validate_protocol(protocol))


def _snapshot_span(entry, snapshot):
    start, finish = _time(entry["started_at"]), _time(entry["finished_at"])
    times = [snapshot.request.arrived_at]
    times.extend(job.finished_at for job in snapshot.jobs if job.finished_at is not None)
    times.extend(attempt.actual_start for attempt in snapshot.attempts if attempt.actual_start is not None)
    times.extend(attempt.actual_finish for attempt in snapshot.attempts if attempt.actual_finish is not None)
    times.extend(_time(record[name]) for record in snapshot.accounting
                 for name in ("measurement_start", "measurement_finish", "journal_recorded_at", "accounted_at"))
    times.extend(_time(decision["recorded_at"]) for decision in snapshot.decisions)
    if snapshot.cancelled_at is not None:
        times.append(snapshot.cancelled_at)
    if min(times) < start or max(times) > finish:
        raise ValueError("Trial timestamps do not enclose its retained workflow evidence")


def _sum(values):
    values = list(values)
    if any(value is None for value in values):
        return None
    try:
        result = math.fsum(values)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _stats(values):
    values = sorted(values)
    def percentile(fraction):
        position = (len(values) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        return values[lower] * (upper - position) + values[upper] * (position - lower) if lower != upper else values[lower]
    count = len(values)
    mean = _sum(value / count for value in values) if count else None
    try:
        deviation = statistics.stdev(values) if count >= 2 else None
    except (OverflowError, ValueError):
        deviation = None
    if deviation is not None and not math.isfinite(deviation):
        deviation = None
    return {"count": count, "mean": mean, "sample_standard_deviation": deviation,
            "minimum": values[0] if count else None, "maximum": values[-1] if count else None,
            "p50": percentile(.5) if count else None, "p90": percentile(.9) if count else None}


def _resources(reports, name, expected_count):
    rows = [report["resources"][name] for report in reports]
    known = [row for row in rows if row["complete"] and row["total"] is not None]
    units = sorted({unit for row in rows for unit in row["known_subtotals"]})
    subtotals = {unit: _sum(row["known_subtotals"].get(unit, 0.) for row in rows) for unit in units}
    complete = len(known) == expected_count and len(units) == 1 and bool(known)
    total = subtotals[units[0]] if complete else None
    provenance = Counter()
    for row in rows:
        provenance.update(row["provenance_counts"])
    return {"total": total, "complete": complete and total is not None,
            "unit": units[0] if len(units) == 1 else None, "known_subtotals": subtotals,
            "known_trials": len(known), "unknown_trials": expected_count - len(known),
            "provenance_counts": dict(sorted(provenance.items())),
            "statistics_by_unit": {unit: _stats(row["total"] for row in known if row["unit"] == unit)
                                   for unit in sorted({row["unit"] for row in known})}}


def _metric(report, name):
    if name in {"response_seconds", "completion_seconds"}:
        return report["timing"][name], "s"
    resource = report["resources"][name]
    return (resource["total"] if resource["complete"] else None), resource["unit"]


def _delta(left, right, name):
    # Every paired denominator remains visible, including failed and missing
    # trials. Comparisons require completed workflows on both sides.
    reason = None
    if left is None or right is None:
        reason = "trial-unfinished"
    elif left["status"] != "completed" or right["status"] != "completed":
        reason = "workflow-not-completed"
    baseline, unit = _metric(left, name) if left is not None else (None, None)
    treatment, other_unit = _metric(right, name) if right is not None else (None, None)
    if reason is None and (baseline is None or treatment is None):
        reason = "metric-unknown"
    if reason is None and unit != other_unit:
        reason = "incompatible-units"
    absolute = treatment - baseline if reason is None else None
    reduction = None
    percentage_reason = reason
    if reason is None:
        if baseline == 0:
            percentage_reason = "zero-baseline"
        else:
            reduction = (1. - treatment / baseline) * 100
            if not math.isfinite(reduction):
                reduction, percentage_reason = None, "percentage-overflow"
    return {"baseline": baseline, "treatment": treatment, "unit": unit if unit == other_unit else None,
            "absolute_delta": absolute, "reduction_percent": reduction,
            "comparison_reason": reason, "percentage_reason": percentage_reason}


@_malformed
def comparison_report(protocol, trials, *, quality_reviews=None):
    """Compare protocol-bound snapshots; unavailable trials remain denominators."""
    protocol = validate_protocol(protocol)
    schedule = schedule_trials(protocol)
    if not isinstance(trials, list) or len(trials) != len(schedule):
        raise ValueError("Comparison requires every scheduled trial")
    reports, retained = {}, []
    previous_finish, unfinished_seen = _time(protocol["created_at"]), False
    for wrapped, identity in zip(trials, schedule):
        if not isinstance(wrapped, dict) or set(wrapped) != {"entry", "snapshot"}:
            raise ValueError("Comparison trials require entry and snapshot only")
        entry, snapshot = wrapped["entry"], wrapped["snapshot"]
        started, finished = _entry(entry, identity)
        if started is not None:
            if unfinished_seen or started < previous_finish:
                raise ValueError("Comparison contradicts the frozen serial trial schedule")
            if finished is not None:
                previous_finish = finished
        if entry["state"] != "finished":
            unfinished_seen = True
        if entry["state"] == "finished":
            if snapshot is None:
                raise ValueError("Finished trial is missing its retained workflow")
            # Validate direct callers' dataclasses as strictly as loaded bundles.
            snapshot = _decode_snapshot(_snapshot_dict(snapshot))
            validate_trial_snapshot(protocol, identity, snapshot)
            _snapshot_span(entry, snapshot)
            report = execution_report(snapshot)
        else:
            if snapshot is not None:
                raise ValueError("An unfinished trial cannot contain a finished workflow bundle")
            report = None
        reports[(identity["case_id"], identity["repeat"], identity["arm_id"])] = report
        retained.append({**deepcopy(entry), "workflow_status": report["status"] if report else None,
                         "execution": report})
    count = len(protocol["dataset"]["cases"]) * protocol["repeats"]
    arms = []
    for arm in protocol["arms"]:
        selected = [row for row in retained if row["arm_id"] == arm["arm_id"]]
        observed = [row["execution"] for row in selected if row["execution"] is not None]
        states = Counter(row["workflow_status"] or row["state"] for row in selected)
        metrics = {}
        for name in ("response_seconds", "completion_seconds"):
            values = [report["timing"][name] for report in observed if report["timing"][name] is not None]
            metrics[name] = {**_stats(values), "unknown_trials": count - len(values)}
        arms.append({"arm_id": arm["arm_id"], "strategy": deepcopy(arm), "scheduled_trials": count,
            "finished_trials": len(observed), "status_counts": dict(sorted(states.items())),
            "attempt_count": sum(report["coverage"]["attempt_count"] for report in observed),
            "accounted_attempt_count": sum(report["coverage"]["accounted_attempts"] for report in observed),
            "response_deadline_met": sum(report["timing"]["response_deadline_met"] is True for report in observed),
            "completion_deadline_met": sum(report["timing"]["completion_deadline_met"] is True for report in observed),
            "timing": metrics, "resources": {name: _resources(observed, name, count) for name in ("cost", "energy", "carbon")},
            "quality": {"value": None, "provenance": "unknown", "reviewed_trials": 0, "unknown_trials": count}})
    paired = []
    baseline_id = protocol["baseline_arm_id"]
    for arm in protocol["arms"]:
        if arm["arm_id"] == baseline_id:
            continue
        pairs = []
        for case in protocol["dataset"]["cases"]:
            for repeat in range(protocol["repeats"]):
                left = reports[(case["case_id"], repeat, baseline_id)]
                right = reports[(case["case_id"], repeat, arm["arm_id"])]
                pairs.append({"case_id": case["case_id"], "repeat": repeat,
                    "baseline_status": left["status"] if left else None,
                    "treatment_status": right["status"] if right else None,
                    "metrics": {name: _delta(left, right, name) for name in _METRICS}})
        statistics_by_metric = {}
        for name in _METRICS:
            values = [pair["metrics"][name] for pair in pairs]
            units = sorted({value["unit"] for value in values if value["absolute_delta"] is not None})
            compatible = len(units) <= 1
            statistics_by_metric[name] = {
                "matched_pairs": sum(value["absolute_delta"] is not None for value in values),
                "unmatched_pairs": sum(value["absolute_delta"] is None for value in values),
                "reason_counts": dict(sorted(Counter(value["comparison_reason"] for value in values
                    if value["comparison_reason"] is not None).items())),
                "units": units, "aggregate_reason": None if compatible else "incompatible-pair-units",
                "absolute_delta": _stats(value["absolute_delta"] for value in values
                    if compatible and value["absolute_delta"] is not None),
                "reduction_percent": _stats(value["reduction_percent"] for value in values
                    if compatible and value["reduction_percent"] is not None),
                "statistics_by_unit": {unit: {
                    "absolute_delta": _stats(value["absolute_delta"] for value in values
                        if value["unit"] == unit and value["absolute_delta"] is not None),
                    "reduction_percent": _stats(value["reduction_percent"] for value in values
                        if value["unit"] == unit and value["reduction_percent"] is not None)} for unit in units}}
        paired.append({"baseline_arm_id": baseline_id, "treatment_arm_id": arm["arm_id"],
                       "scheduled_pairs": len(pairs), "metrics": statistics_by_metric, "pairs": pairs})
    reasons = ["end-to-end-quality-unreviewed"]
    if protocol["experiment_mode"] == "simulation":
        reasons.append("simulation-not-live-evidence")
    if protocol["dataset"]["split"] != "final":
        reasons.append("pilot-dataset-not-final-evaluation")
    if any(row["state"] != "finished" for row in retained):
        reasons.append("unfinished-trials")
    if any(not arm["resources"][name]["complete"] for arm in arms for name in ("cost", "energy", "carbon")):
        reasons.append("incomplete-resource-coverage")
    result = {"schema_version": 1, "record_type": "MatchedPolicyComparison",
        "protocol_id": protocol["protocol_id"], "protocol_fingerprint": protocol["fingerprint"],
        "experiment_mode": protocol["experiment_mode"], "dataset_id": protocol["dataset"]["dataset_id"],
        "dataset_split": protocol["dataset"]["split"], "source_fingerprint": fingerprint(protocol["source_manifest"]),
        "scheduled_trials": len(schedule), "finished_trials": sum(row["state"] == "finished" for row in retained),
        "baseline_arm_id": baseline_id, "arms": arms, "paired_comparisons": paired, "trials": retained,
        "release_evidence": {"eligible": False, "reasons": reasons},
        "notes": ["Statistics and interpolated percentiles are descriptive; no confidence interval or stable-tail claim is implied.",
                  "Failed, infeasible and unfinished trials remain in coverage denominators; paired metrics require completed workflows.",
                  "Absolute deltas are treatment minus baseline; positive reduction percentages mean lower treatment observations.",
                  "A zero or unknown baseline has no reduction percentage. CO2 and CO2e are never pooled.",
                  "Serial randomized blocks are not simultaneous counterfactuals; model residency and host conditions may vary.",
                  "Per-step quality proxies do not establish reviewed end-to-end quality or quality-preserving savings."]}
    result["fingerprint"] = fingerprint(result)
    if "output_quality" in protocol or quality_reviews is not None:
        from workflow.workflow_quality import validate_review_set, review_passes
        approved = {}
        if quality_reviews is not None:
            checked = validate_review_set(quality_reviews, protocol, trials)
            approved = {review["trial_id"]: review for review in checked["reviews"]}
        for row in result["trials"]:
            review = approved.get(row["trial_id"])
            row["output_quality"] = {"value": review["score"] if review else None,
                "provenance": "human-reviewed" if review else "unknown",
                "passed": review_passes(review, protocol["output_quality"]) if review else None}
        for arm in result["arms"]:
            rows = [row["output_quality"] for row in result["trials"] if row["arm_id"] == arm["arm_id"]]
            scores = [row["value"] for row in rows if row["value"] is not None]
            arm["quality"] = {"value": min(scores) if scores and len(scores) == len(rows) else None,
                "provenance": "human-reviewed" if scores else "unknown", "reviewed_trials": len(scores),
                "unknown_trials": len(rows) - len(scores), "passed_trials": sum(row["passed"] is True for row in rows),
                "failed_trials": sum(row["passed"] is False for row in rows), "statistics": _stats(scores)}
        result["quality_review_coverage"] = {"reviewed_trials": len(approved),
            "scheduled_trials": len(trials), "complete": bool(trials) and len(approved) == len(trials),
            "review_set_fingerprint": quality_reviews["fingerprint"] if quality_reviews else None,
            "aggregation": "minimum-step-mean.v1"}
        for paired in result["paired_comparisons"]:
            deltas = []
            for pair in paired["pairs"]:
                rows = {row["arm_id"]: row for row in result["trials"]
                    if row["case_id"] == pair["case_id"] and row["repeat"] == pair["repeat"]}
                left = rows[paired["baseline_arm_id"]]["output_quality"]["value"]
                right = rows[paired["treatment_arm_id"]]["output_quality"]["value"]
                delta = right - left if left is not None and right is not None else None
                pair["quality"] = {"baseline": left, "treatment": right, "absolute_delta": delta}
                if delta is not None:
                    deltas.append(delta)
            paired["quality"] = {"matched_pairs": len(deltas), "unmatched_pairs": len(paired["pairs"]) - len(deltas),
                                  "absolute_delta": _stats(deltas)}
        if len(approved) == len(trials) and trials:
            result["release_evidence"]["reasons"].remove("end-to-end-quality-unreviewed")
            result["release_evidence"]["reasons"].append("release-validation-outstanding")
        if any(not review_passes(review, protocol["output_quality"]) for review in approved.values()):
            result["release_evidence"]["reasons"].append("reviewed-output-quality-below-floor")
        result["fingerprint"] = fingerprint({key: value for key, value in result.items() if key != "fingerprint"})
    return result


def _read_evaluation(directory, supplied=None):
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("Evaluation must be a regular directory")
    files = {}
    def read(name):
        _relative(name)
        if supplied is not None:
            if name not in supplied:
                raise ValueError("Portable comparison omits required evidence")
            data = supplied[name]
        else:
            data = _safe_file(directory, name).read_bytes()
        files[name] = data
        return data
    protocol = validate_protocol(_json(read("protocol.json")))
    journal = _journal(_json(read("evaluation.json")), protocol)
    for name, digest in protocol["source_manifest"].items():
        if hashlib.sha256(read("source/" + name)).hexdigest() != digest:
            raise ValueError("Retained implementation source differs from the frozen protocol")
    trials = []
    for entry in journal["trials"]:
        snapshot = None
        if entry["state"] == "finished":
            base = entry["bundle"]
            if supplied is None:
                parent = _safe_file(directory, base + "/manifest.json").parent
                if {path.name for path in parent.iterdir()} != {"manifest.json", "workflow.json"}:
                    raise ValueError("Trial replay bundle must contain exactly its two evidence files")
            manifest = _json(read(base + "/manifest.json"))
            workflow_bytes = read(base + "/workflow.json")
            if (not isinstance(manifest, dict) or set(manifest) != {
                    "schema_version", "bundle_version", "record_type", "workflow_id", "files"}
                    or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 3
                    or type(manifest["bundle_version"]) is not int or manifest["bundle_version"] not in {1, 2, 3}
                    or manifest["record_type"] != "DurableWorkflowReplayManifest"
                    or manifest["workflow_id"] != entry["workflow_id"]
                    or manifest["files"] != {"workflow.json": hashlib.sha256(workflow_bytes).hexdigest()}):
                raise ValueError("Trial replay manifest/checksum differs from retained workflow")
            payload = _json(workflow_bytes)
            if not isinstance(payload, dict) or payload.get("bundle_version") != manifest["bundle_version"]:
                raise ValueError("Trial replay bundle versions differ")
            snapshot = _decode_snapshot(payload)
        trials.append({"entry": entry, "snapshot": snapshot})
    has_reviews = ("quality_reviews.json" in supplied if supplied is not None
                   else (directory / "quality_reviews.json").exists())
    reviews = _json(read("quality_reviews.json")) if has_reviews else None
    return comparison_report(protocol, trials, quality_reviews=reviews), files


@_malformed
def load_evaluation(directory):
    """Read a live/interrupted journal as one offline snapshot; never resume it."""
    return _read_evaluation(directory)[0]


def _encode(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


@_malformed
def export_comparison(evaluation_dir, output):
    """Copy a validated snapshot into a new self-contained checksummed package."""
    report, files = _read_evaluation(evaluation_dir)
    files["comparison.json"] = _encode(report)
    manifest = {"schema_version": 1, "record_type": "PolicyComparisonManifest",
        "protocol_fingerprint": report["protocol_fingerprint"],
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    manifest["fingerprint"] = fingerprint(manifest)
    output = Path(output)
    if output.exists():
        raise FileExistsError("Comparison output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    output_root = output.resolve()
    created, directories = [], [output]
    try:
        for name, data in [*sorted(files.items()), ("manifest.json", _encode(manifest))]:
            path = output.joinpath(*_relative(name).parts)
            if not path.resolve().is_relative_to(output_root):
                raise ValueError("Comparison export escaped its newly reserved directory")
            missing, parent = [], path.parent
            while parent != output and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                directories.append(directory)
            with path.open("xb") as stream:
                created.append(path)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        return output
    except BaseException:
        for path in reversed(created):
            if path.resolve().is_relative_to(output_root):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        for directory in reversed(directories):
            if not directory.is_symlink() and directory.resolve().is_relative_to(output_root):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        raise


@_malformed
def load_comparison(bundle):
    """Verify checksums and recompute a portable comparison entirely offline."""
    root = Path(bundle)
    manifest = _json(_safe_file(root, "manifest.json").read_bytes())
    _sealed(manifest, _MANIFEST_FIELDS, "PolicyComparisonManifest")
    if not isinstance(manifest["files"], dict) or not 3 <= len(manifest["files"]) <= 25000:
        raise ValueError("Comparison manifest has unsupported evidence coverage")
    files = {}
    for name, digest in manifest["files"].items():
        if not isinstance(digest, str) or not _SHA.fullmatch(digest) or name == "manifest.json":
            raise ValueError("Comparison requires SHA256 file entries")
        data = _safe_file(root, name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Comparison file checksum mismatch")
        files[name] = data
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Comparison cannot contain symlinks")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files) | {"manifest.json"}:
        raise ValueError("Comparison contains unlisted or missing evidence")
    report, required = _read_evaluation(root, supplied=files)
    if (set(files) != set(required) | {"comparison.json"}
            or _json(files["comparison.json"]) != report
            or manifest["protocol_fingerprint"] != report["protocol_fingerprint"]):
        raise ValueError("Comparison differs from independently recomputed protocol-bound evidence")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="Read an evaluation journal offline")
    report.add_argument("--evaluation", required=True)
    export = commands.add_parser("export", help="Freeze journal evidence into a new portable comparison")
    export.add_argument("--evaluation", required=True)
    export.add_argument("--output", required=True)
    inspect = commands.add_parser("inspect", help="Verify and recompute a portable comparison")
    inspect.add_argument("--bundle", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            path = export_comparison(args.evaluation, args.output)
            print(json.dumps({"comparison": str(path)}))
        else:
            result = load_evaluation(args.evaluation) if args.command == "report" else load_comparison(args.bundle)
            print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"error_code": "invalid-policy-comparison-evidence"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
