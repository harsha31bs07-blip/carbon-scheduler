"""The offline demonstration cannot become live inference or human quality."""

import pytest

from workflow.demo_evidence import create_demo
from workflow.policy_comparison import load_comparison
from workflow.review_workflow_quality import export_reviews


def test_scripted_demo_executes_and_replays_without_provider_calls(tmp_path, monkeypatch):
    import requests
    import workflow.deployment_clients as clients
    monkeypatch.setattr(clients, "call_deployment", lambda *a, **k: pytest.fail("Offline demo called a provider"))
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("Offline demo performed HTTP GET"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("Offline demo performed HTTP POST"))
    result = create_demo(tmp_path / "portable", work_directory=tmp_path / "evaluation")
    assert result["experiment_mode"] == "simulation" and not result["inference_performed"]
    assert result["finished_trials"] == 4 and not result["release_eligible"] and result["human_reviews_generated"] == 0
    report = load_comparison(result["bundle"])
    assert report["finished_trials"] == 4 and report["quality_review_coverage"]["reviewed_trials"] == 0
    assert all(row["workflow_status"] == "completed" for row in report["trials"])
    assert all(arm["quality"]["value"] is None and arm["resources"]["energy"]["total"] is None for arm in report["arms"])
    assert export_reviews(result["evaluation"], tmp_path / "packet")["reviewable_trials"] == 4
    with pytest.raises(FileExistsError):
        create_demo(result["bundle"])
