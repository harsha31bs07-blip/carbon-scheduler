"""Human review packets feed frozen profiles without mutating raw evidence."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from data.profiles import CONTEXT_SOURCE, estimate_step, evaluate_profiles
from data.quality_reviews import fingerprint, load_rubric, review_template
from workflow.profile_report import freeze_profiles, main as freeze_main
from workflow.review_quality import export_reviews, main, validate_reviews


@pytest.fixture
def calibration(tmp_path, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("Review made a network call"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("Review made an inference call"))
    record = {
        "schema_version": 1, "record_type": "CalibrationTrial", "trial_id": "trial-a",
        "case_id": "cal-account-01", "split": "calibration", "context_source": CONTEXT_SOURCE,
        "model_id": "small", "deployment_id": "local", "deployment_fingerprint": "local-v1",
        "step_id": "draft", "task_type": "support-reply", "workload_version": "connected-v1",
        "input_size_estimate": 200, "output_limit_tokens": 128, "enable_thinking": False,
        "warmup": False, "cold_start": False, "status": "completed", "output_valid": True,
        "completion_reason": "stop", "latency_seconds": 4., "load_seconds": .1,
        "input_tokens": 45, "output_tokens": 20, "quality_score": None, "quality_source": "unknown",
        "energy_kwh": None, "energy_provenance": "unknown", "energy_source": None,
        "carbon_g": None, "carbon_provenance": "unknown", "carbon_source": None,
        "cost_usd": None, "cost_provenance": "unknown", "cost_source": None,
        "prompt": "Answer this account lockout ticket using KB-ACCOUNT-002.",
        "source_ids": ["KB-ACCOUNT-002"], "result": {"text": "Wait 15 minutes. [KB-ACCOUNT-002]"},
    }
    directory = tmp_path / "calibration"
    directory.mkdir()
    (directory / "trial-a.json").write_text(json.dumps(record), encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps({"schema_version": 1,
        "record_type": "CalibrationRun", "run_id": "run-a", "status": "completed",
        "split": "calibration", "retained_trials": 1}), encoding="utf-8")
    return directory, record


def approved(record):
    review = review_template(record, load_rubric())
    review.update(status="approved", review_method="human", reviewer="Fixture human reviewer",
                  reviewed_at="2026-10-20T12:00:00Z", score=1., rationale="Judged against the provided article.")
    for value in review["criteria"].values():
        value.update(score=1., rationale="Meets this criterion in the fixture output.")
    return review


def review_file(tmp_path, reviews, name="reviews.json"):
    path = tmp_path / name
    path.write_text(json.dumps(reviews), encoding="utf-8")
    return path


def test_export_has_blank_judgments_exact_evidence_and_no_overwrite(calibration, tmp_path):
    directory, record = calibration
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    output = tmp_path / "packet"
    manifest = export_reviews([directory], output)
    assert manifest["reviewable_trials"] == 1
    packet = json.loads((output / "packet.json").read_text(encoding="utf-8"))
    templates = json.loads((output / "reviews.pending.json").read_text(encoding="utf-8"))
    assert packet["items"][0]["trial"] == record
    assert packet["recorded_contexts"][str(directory)]["status"] == "unavailable"
    assert templates[0]["status"] == "pending" and templates[0]["score"] is None
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match="new directory"):
        export_reviews([directory], output)
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}


def test_approved_review_trains_quality_with_embedded_audit_and_raw_trials_unchanged(calibration, tmp_path):
    directory, record = calibration
    before = (directory / "trial-a.json").read_bytes()
    reviews = review_file(tmp_path, [approved(record)])
    evidence = validate_reviews([directory], [reviews])
    assert evidence["coverage"]["reviewed_trials"] == 1
    output = tmp_path / "reviewed-profiles"
    profiles = freeze_profiles([directory], output, review_paths=[reviews])
    assert profiles["profiles"][0]["metrics"]["accuracy"]["mean"] == 1.
    assert profiles["profiles"][0]["sample_counts"]["reviewed_quality"] == 1
    assert profiles["profiles"][0]["metrics"]["accuracy"]["provenance"] == "estimated"
    estimate = estimate_step(profiles, **{key: record[key] for key in (
        "deployment_fingerprint", "model_id", "step_id", "workload_version", "input_size_estimate",
        "output_limit_tokens", "enable_thinking", "task_type")})
    assert estimate.accuracy.value is None  # One recorded judgment is below the default pilot coverage gate.
    evaluate_profiles(profiles, [])  # Existing strict profile reader accepts the new evidence identity.
    saved = json.loads((output / "quality_reviews.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert saved["reviews"] == [approved(record)]
    assert manifest["quality_review_evidence_sha256"] == hashlib.sha256((output / "quality_reviews.json").read_bytes()).hexdigest()
    assert manifest["quality_eligibility"] == {"eligible_profiles": 0, "profile_count": 1}
    assert (directory / "trial-a.json").read_bytes() == before


def test_same_score_with_changed_review_rationale_changes_profile_identity(calibration, tmp_path):
    directory, record = calibration
    review = approved(record)
    original = review_file(tmp_path, [review], "first.json")
    first = freeze_profiles([directory], tmp_path / "first", review_paths=[original])
    review["rationale"] = "A distinct attributable judgment with the same numerical score."
    changed = review_file(tmp_path, [review], "changed.json")
    second = freeze_profiles([directory], tmp_path / "second", review_paths=[changed])
    assert first["fingerprint"] != second["fingerprint"]
    assert first["profiles"][0]["metrics"]["accuracy"] == second["profiles"][0]["metrics"]["accuracy"]


@pytest.mark.parametrize("change", ["pending", "output", "duplicate", "unknown-field", "non-list"])
def test_bad_reviews_fail_before_creating_aggregate(calibration, tmp_path, change):
    directory, record = calibration
    reviews = [approved(record)]
    if change == "pending":
        reviews = [review_template(record, load_rubric())]
    elif change == "output":
        record["result"]["text"] = "Edited output."
        (directory / "trial-a.json").write_text(json.dumps(record), encoding="utf-8")
    elif change == "duplicate":
        reviews += deepcopy(reviews)
    elif change == "unknown-field":
        reviews[0]["hidden_override"] = True
    else:
        reviews = {"reviews": reviews}
    path = review_file(tmp_path, reviews)
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError):
        freeze_profiles([directory], output, review_paths=[path])
    assert not output.exists()


def test_review_and_freeze_cli_validate_without_dispatch(calibration, tmp_path, capsys):
    directory, record = calibration
    path = review_file(tmp_path, [approved(record)])
    assert main(["export", "--input", str(directory), "--output", str(tmp_path / "packet")]) == 0
    assert main(["validate", "--input", str(directory), "--reviews", str(path)]) == 0
    assert freeze_main(["--input", str(directory), "--reviews", str(path), "--output", str(tmp_path / "frozen")]) == 0
    assert "reviewed_trials" in capsys.readouterr().out


def test_ambiguous_trial_json_rejected_before_export(calibration, tmp_path):
    directory, _ = calibration
    path = directory / "trial-a.json"
    original = path.read_text(encoding="utf-8")
    path.write_text(original[:-1] + ', "quality_score": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        export_reviews([directory], tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


def test_packet_context_is_bound_to_experiment_snapshot(calibration, tmp_path):
    directory, _ = calibration
    root = Path(__file__).resolve().parents[1]
    hashes = {}
    for name in ("data/workloads/support_tickets.v1.json", "data/workloads/support_kb.v1.json"):
        data = (root / name).read_bytes()
        target = directory / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    snapshot = {"schema_version": 1, "record_type": "CalibrationSourceSnapshot",
                "files": hashes, "fingerprint": fingerprint(hashes)}
    (directory / "source_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_snapshot_fingerprint"] = fingerprint(hashes)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    export_reviews([directory], tmp_path / "packet")
    packet = json.loads((tmp_path / "packet/packet.json").read_text(encoding="utf-8"))
    assert packet["recorded_contexts"][str(directory)]["manifest_binding"] == "source-snapshot"
    # A self-consistent snapshot from elsewhere still cannot replace this run's.
    hashes["unrelated.py"] = "0" * 64
    snapshot["fingerprint"] = fingerprint(hashes)
    (directory / "source_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValueError, match="experiment manifest"):
        export_reviews([directory], tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


def test_raw_review_hash_is_validated_before_profile_fitting(calibration, tmp_path):
    directory, record = calibration
    record.update(quality_score=.8, quality_source="human-review:fixture-v1",
                  quality_review_fingerprint="invalid")
    (directory / "trial-a.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        freeze_profiles([directory], tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


def test_export_real_pilot_snapshot_formats_without_creating_judgments(tmp_path):
    root = Path(__file__).resolve().parents[1]
    directories = [root / "results/benchmarks" / name for name in
                   ("m3-local-calibration-20261019", "m3-local-classify-followup-20261019")]
    manifest = export_reviews(directories, tmp_path / "retained-pilot")
    assert manifest["retained_trials"] == 99
    assert manifest["reviewable_trials"] == 80
    packet = json.loads((tmp_path / "retained-pilot/packet.json").read_text(encoding="utf-8"))
    contexts = list(packet["recorded_contexts"].values())
    assert contexts[0]["snapshot_hash_method"] == "legacy-json-default-separators-v1"
    assert contexts[0]["manifest_binding"] == "fixture-fingerprints"
    assert contexts[1]["snapshot_hash_method"] == "canonical-json-v1"
    assert contexts[1]["manifest_binding"] == "source-snapshot"
    templates = json.loads((tmp_path / "retained-pilot/reviews.pending.json").read_text(encoding="utf-8"))
    assert all(review["status"] == "pending" and review["score"] is None for review in templates)
