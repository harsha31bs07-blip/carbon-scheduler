"""Human-review contracts use synthetic judgments solely as test fixtures."""

from copy import deepcopy
from datetime import timedelta
import json
import subprocess
import sys

import pytest

from data.quality_reviews import load_json, fingerprint
from workflow.evaluation_protocol import seal_protocol
from workflow.workflow_quality import (default_review_config, review_template, validate_review,
    make_review_set, validate_review_set, review_passes)
from workflow.review_workflow_quality import export_reviews, import_reviews
from workflow.policy_comparison import comparison_report, export_comparison, load_comparison, load_evaluation
from test_evaluation_protocol import protocol, NOW
from test_policy_comparison import _build_evaluation


_TEMPLATE = None

@pytest.fixture
def reviewed(tmp_path, protocol):
    global _TEMPLATE
    if _TEMPLATE is None:
        value = seal_protocol({**protocol, "repeats": 1, "output_quality": default_review_config()})
        root, frozen, journal, trials = _build_evaluation(tmp_path / "template", value)
        files = {path.relative_to(root): path.read_bytes() for path in root.rglob("*")
                 if path.is_file() and not path.name.endswith((".sqlite3", "-wal", "-shm"))}
        _TEMPLATE = files, frozen, journal, trials
    files, frozen, journal, trials = _TEMPLATE
    root = tmp_path / "evaluation"
    root.mkdir()
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root, deepcopy(frozen), deepcopy(journal), deepcopy(trials)


def approved(protocol, wrapped, score=1.):
    value = review_template(protocol, wrapped["entry"], wrapped["snapshot"])
    value.update(status="approved", reviewer="Test human fixture", reviewed_at=(NOW + timedelta(hours=1)).isoformat(),
                 score=score, rationale="Synthetic fixture judgment; not a real evaluation review")
    for step in value["steps"].values():
        step["score"] = score
        for criterion in step["criteria"].values():
            criterion.update(score=score, rationale="Synthetic criterion rationale")
    return value


AS_OF = (NOW + timedelta(hours=2)).isoformat()


def test_blank_packet_never_creates_quality_and_import_roundtrips(reviewed, tmp_path):
    root, protocol, _, trials = reviewed
    manifest = export_reviews(root, tmp_path / "packet")
    templates = load_json(tmp_path / "packet/reviews.pending.json")
    assert manifest["reviewable_trials"] == 4 and all(value["score"] is None for value in templates)
    assert all(arm["quality"]["value"] is None for arm in load_evaluation(root)["arms"])
    with pytest.raises(ValueError, match="approved human"):
        import_reviews(root, templates, as_of=AS_OF)
    reviews = [approved(protocol, wrapped) for wrapped in trials]
    imported = import_reviews(root, reviews, as_of=AS_OF)
    report = load_evaluation(root)
    assert report["quality_review_coverage"]["complete"]
    assert all(arm["quality"]["value"] == 1 for arm in report["arms"])
    assert not report["release_evidence"]["eligible"]  # Simulation remains simulation.
    assert "simulation-not-live-evidence" in report["release_evidence"]["reasons"]
    assert import_reviews(root, reviews, as_of=AS_OF) == imported
    bundle = export_comparison(root, tmp_path / "portable")
    assert load_comparison(bundle) == report
    with pytest.raises(ValueError, match="immutable"):
        import_reviews(bundle, reviews, as_of=AS_OF)


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(workflow_sha256="0" * 64), lambda v: v.update(outputs_sha256="0" * 64),
    lambda v: v.update(protocol_fingerprint="0" * 64), lambda v: v.update(rubric_sha256="0" * 64),
    lambda v: v.update(status="pending"), lambda v: v.update(review_method="model-judge"),
    lambda v: v.update(reviewer=""), lambda v: v.update(rationale=""),
    lambda v: v.update(reviewed_at=NOW.isoformat()),
    lambda v: v.update(reviewed_at=(NOW + timedelta(days=2)).isoformat()),
    lambda v: v.update(score=.1), lambda v: v["steps"]["draft"].update(score=.1),
    lambda v: v["steps"]["draft"]["criteria"].clear(),
    lambda v: v["steps"]["classify"]["criteria"]["category_correct"].update(score=True),
    lambda v: v["steps"]["classify"]["criteria"]["category_correct"].update(rationale=""),
])
def test_attribution_content_timing_and_arithmetic_tampering_rejected(reviewed, mutate):
    _, protocol, _, trials = reviewed
    value = approved(protocol, trials[0])
    mutate(value)
    with pytest.raises(ValueError):
        validate_review(value, protocol, trials[0]["entry"], trials[0]["snapshot"], as_of=AS_OF)


def test_partial_failing_quality_and_additive_import_preserve_coverage(reviewed):
    root, protocol, _, trials = reviewed
    first = approved(protocol, trials[0], .4)
    assert not review_passes(first, protocol["output_quality"])
    imported = import_reviews(root, [first], as_of=AS_OF)
    report = load_evaluation(root)
    assert report["quality_review_coverage"]["reviewed_trials"] == 1
    assert not report["quality_review_coverage"]["complete"]
    assert "reviewed-output-quality-below-floor" in report["release_evidence"]["reasons"]
    replacement = approved(protocol, trials[0], 1)
    with pytest.raises(ValueError, match="overwritten"):
        import_reviews(root, [replacement], as_of=AS_OF)
    revised = import_reviews(root, [approved(protocol, trials[1])], as_of=AS_OF)
    assert len(revised["reviews"]) == 2
    assert load_json(root / "quality-review-history" / (imported["fingerprint"] + ".json")) == imported
    with pytest.raises(ValueError, match="Duplicate"):
        make_review_set(protocol, trials, [first, first], as_of=AS_OF)


def test_rehashed_reviewset_cannot_change_bound_score(reviewed):
    _, protocol, _, trials = reviewed
    value = make_review_set(protocol, trials, [approved(protocol, trials[0])], as_of=AS_OF)
    value["reviews"][0]["score"] = .5
    value["fingerprint"] = fingerprint({key: item for key, item in value.items() if key != "fingerprint"})
    with pytest.raises(ValueError):
        validate_review_set(value, protocol, trials)


def test_legacy_protocol_requires_new_freeze_before_review(reviewed):
    _, protocol, _, trials = reviewed
    del protocol["output_quality"]
    with pytest.raises(ValueError, match="freeze|frozen"):
        make_review_set(protocol, trials, [], as_of=AS_OF)


def test_review_packet_cli_is_offline_and_never_scores_outputs(reviewed, tmp_path):
    root, _, _, _ = reviewed
    script = """
import builtins, socket, sys, time
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {'workflow.worker', 'workflow.planned_worker', 'workflow.dispatch', 'requests', 'codecarbon'}:
        raise AssertionError('Review imported a provider transport')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
def forbidden(*args, **kwargs):
    raise AssertionError('Review contacted a provider or waited')
socket.socket = forbidden
time.sleep = forbidden
from workflow.review_workflow_quality import main
raise SystemExit(main(['export', '--evaluation', sys.argv[1], '--output', sys.argv[2]]))
"""
    target = tmp_path / "offline-packet"
    result = subprocess.run([sys.executable, "-c", script, str(root), str(target)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["reviewable_trials"] == 4
    assert all(review["score"] is None for review in load_json(target / "reviews.pending.json"))
