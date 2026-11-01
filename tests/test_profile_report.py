"""Frozen aggregates reject unsuitable journals and separate audit/statistical identity."""

from copy import deepcopy
import hashlib
import json

import pytest

from data.profiles import CONTEXT_SOURCE
from workflow.profile_report import freeze_profiles


def trial(trial_id, **changes):
    return {
        "schema_version": 1, "record_type": "CalibrationTrial", "trial_id": trial_id,
        "split": "calibration", "context_source": CONTEXT_SOURCE, "model_id": "llama",
        "deployment_id": "local", "deployment_fingerprint": "local-config-v1", "step_id": "draft",
        "task_type": "support-reply", "workload_version": "connected-v1", "input_size_estimate": 200,
        "output_limit_tokens": 128, "enable_thinking": False, "warmup": False, "cold_start": False,
        "status": "completed", "output_valid": True, "completion_reason": "stop",
        "latency_seconds": 4., "load_seconds": .1, "input_tokens": 45, "output_tokens": 20,
        "quality_score": None, "quality_source": "unknown", "energy_kwh": None,
        "energy_provenance": "unknown", "energy_source": None, "carbon_g": None,
        "carbon_provenance": "unknown", "carbon_source": None, "cost_usd": None,
        "cost_provenance": "unknown", "cost_source": None,
        **changes,
    }


def journal(tmp_path, name, records, **manifest_changes):
    directory = tmp_path / name
    directory.mkdir()
    manifest = {"schema_version": 1, "record_type": "CalibrationRun", "run_id": name,
                "status": "completed", "split": "calibration", "retained_trials": len(records),
                **manifest_changes}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for index, record in enumerate(records):
        (directory / f"trial-{index}.json").write_text(json.dumps(record), encoding="utf-8")
    return directory


def test_duplicate_input_directory_or_trial_identity_never_creates_output(tmp_path):
    first = journal(tmp_path, "run-a", [trial("same")])
    second = journal(tmp_path, "run-b", [trial("same", latency_seconds=30)])
    for directories in ([first, first], [first, second]):
        output = tmp_path / "must-not-exist"
        with pytest.raises(ValueError, match="duplicate"):
            freeze_profiles(directories, output)
        assert not output.exists()


@pytest.mark.parametrize("manifest_changes", [
    {"status": "running"}, {"status": "interrupted"}, {"split": "held_out"},
    {"retained_trials": 2}, {"retained_trials": True}, {"schema_version": 2},
    {"schema_version": True}, {"run_id": ""},
])
def test_incomplete_heldout_mismatched_or_invalid_runs_rejected_before_output(tmp_path, manifest_changes):
    directory = journal(tmp_path, "run", [trial("a")], **manifest_changes)
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError):
        freeze_profiles([directory], output)
    assert not output.exists()


def test_heldout_trial_cannot_enter_aggregate_even_under_calibration_manifest(tmp_path):
    directory = journal(tmp_path, "run", [trial("training"), trial("held", split="held_out", latency_seconds=999)])
    with pytest.raises(ValueError, match="Held-out"):
        freeze_profiles([directory], tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


def test_empty_inputs_empty_runs_and_duplicate_trials_within_run_rejected(tmp_path):
    with pytest.raises(ValueError):
        freeze_profiles([], tmp_path / "empty-input")
    empty = journal(tmp_path, "empty", [])
    with pytest.raises(ValueError):
        freeze_profiles([empty], tmp_path / "empty-output")
    duplicates = journal(tmp_path, "duplicate", [trial("same"), trial("same")])
    with pytest.raises(ValueError, match="unique|duplicate"):
        freeze_profiles([duplicates], tmp_path / "duplicate-output")


def test_freeze_never_overwrites_existing_evidence(tmp_path):
    directory = journal(tmp_path, "run", [trial("a")])
    output = tmp_path / "frozen"
    freeze_profiles([directory], output)
    saved = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(ValueError, match="new directory"):
        freeze_profiles([directory], output)
    assert saved == {path.name: path.read_bytes() for path in output.iterdir()}


def test_profiles_ignore_directory_order_grouping_and_journal_metadata(tmp_path):
    a, b = trial("a", latency_seconds=3.), trial("b", latency_seconds=5.)
    first = journal(tmp_path, "run-a", [a])
    second = journal(tmp_path, "run-b", [b])
    together = journal(tmp_path, "regrouped", [b, a])
    forward = freeze_profiles([first, second], tmp_path / "forward")
    reverse = freeze_profiles([second, first], tmp_path / "reverse")
    regrouped = freeze_profiles([together], tmp_path / "regrouped-output")
    assert forward == reverse == regrouped
    assert forward["profiles"][0]["metrics"]["latency"]["median"] == 4.


def test_warmup_changes_affect_audit_hash_but_not_fitted_profile_identity(tmp_path):
    baseline = journal(tmp_path, "baseline", [trial("a")])
    with_warmup = journal(tmp_path, "with-warmup", [
        trial("a", arbitrary_note="journal metadata is not a model feature"),
        trial("warm", warmup=True, latency_seconds=9999, input_size_estimate=99999),
    ])
    initial = freeze_profiles([baseline], tmp_path / "initial")
    excluded = freeze_profiles([with_warmup], tmp_path / "excluded")
    assert initial["fingerprint"] == excluded["fingerprint"]
    assert initial["profiles"] == excluded["profiles"]
    assert excluded["summary"]["calibration_warmups_excluded"] == 1
    first_manifest = json.loads((tmp_path / "initial" / "manifest.json").read_text())
    next_manifest = json.loads((tmp_path / "excluded" / "manifest.json").read_text())
    assert first_manifest["schema_version"] == next_manifest["schema_version"] == 2
    assert first_manifest["identity_method"] == next_manifest["identity_method"] == "eligible-profile-content-v1"
    assert first_manifest["inputs"][0]["trials_fingerprint"] != next_manifest["inputs"][0]["trials_fingerprint"]


def test_failed_attempts_stay_in_profiles_and_source_hashes_remain_verifiable(tmp_path):
    records = [trial("good", latency_seconds=3), trial("failed", status="failed", output_valid=False,
        completion_reason=None, latency_seconds=40, input_tokens=None, output_tokens=None)]
    directory = journal(tmp_path, "run", records)
    profiles = freeze_profiles([directory], tmp_path / "frozen")
    counts = profiles["profiles"][0]["sample_counts"]
    assert counts["attempts"] == 2 and counts["failed"] == 1
    assert counts["valid_natural_completions"] == 1
    assert profiles["profiles"][0]["metrics"]["latency"]["median"] == 3
    assert profiles["profiles"][0]["all_attempt_latency"]["maximum"] == 40
    manifest = json.loads((tmp_path / "frozen" / "manifest.json").read_text())
    digest = hashlib.sha256(json.dumps(sorted(records, key=lambda item: item["trial_id"]),
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    assert manifest["inputs"][0]["trials_fingerprint"] == digest
    assert manifest["inputs"][0]["manifest_fingerprint"] == hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    changed = deepcopy(records)
    changed[1]["latency_seconds"] = 50
    changed_dir = journal(tmp_path, "changed", changed)
    assert freeze_profiles([changed_dir], tmp_path / "changed-frozen")["fingerprint"] != profiles["fingerprint"]
