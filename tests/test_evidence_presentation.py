"""UI explanations preserve missing metrics and cached evidence integrity."""

import json

import pytest

from workflow.dashboard.evidence_cache import retained_evidence
from workflow.dashboard.evidence_view import load_evidence
from workflow.dashboard.explanations import paired_rows, planning_rows
from test_evaluation_protocol import protocol
from test_policy_comparison import evaluation


def test_cached_report_is_invalidated_when_evidence_changes(evaluation):
    directory, *_ = evaluation
    state = {}
    load_evidence(state, directory, "Evaluation directory")
    assert retained_evidence(state)
    path = directory / "evaluation.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed"):
        retained_evidence(state)
    assert "validated_comparison" not in state


def test_unknown_and_zero_baselines_remain_missing_in_tables():
    from pathlib import Path
    report = json.loads((Path(__file__).resolve().parents[1] /
        "results/benchmarks/a2-offline-dashboard-20261101/comparison.json").read_text())
    rows = {row["Metric"]: row for row in paired_rows(report["paired_comparisons"][0])}
    assert rows["cost"]["Mean reduction (%)"] is None
    assert rows["energy"]["Mean difference"] is None
    assert rows["energy"]["Unmatched pairs"] == 2


def test_missing_quality_has_actionable_explanation_without_invented_scores():
    rows = planning_rows({"result": {"rejection_counts": {"unknown-accuracy": 2}}})
    assert rows[0]["Count"] == 2 and "genuine rubric reviews" in rows[0]["Next action"]
