from pathlib import Path
import json

import pytest

from shared.deployments import Deployment, DeploymentRegistry
from workflow import evaluate_smoke as smoke
from workflow.deployment_clients import DeploymentTransientError


def registry():
    return DeploymentRegistry([Deployment("edge", "ollama", "http://localhost:11434", {"small": "exact:tag"})])


def items():
    return [{"id": "one", "category": "classification", "prompt": "Classify this example.",
             "reference_answer": "Billing", "rubric": {"expected_keywords": ["Billing"]}}]


def test_live_proxy_never_becomes_measured_semantic_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "load_eval_set", items)
    calls = []

    def provider(deployment, model, prompt, **settings):
        calls.append((deployment, model, prompt, settings))
        return {"text": "Billing", "reported_model_id": "exact:tag", "completion_reason": "stop",
                "input_tokens": 10, "output_tokens": 1}

    out = tmp_path / "smoke.json"
    result = smoke.evaluate_smoke(registry(), "edge", ["small"], output=out, dispatcher=provider, progress=False)
    assert result["status"] == "completed" and result["is_measured"] is False
    assert result["semantic_quality_status"] == "unreviewed"
    assert result["records"][0]["score"] == 1
    assert result["records"][0]["item"]["rubric"] == items()[0]["rubric"]
    assert calls[0][3]["max_attempts"] == 1 and calls[0][3]["enable_thinking"] is False
    assert json.loads(out.read_text()) == result
    with pytest.raises(FileExistsError):
        smoke.evaluate_smoke(registry(), "edge", ["small"], output=out, dispatcher=provider, progress=False)


@pytest.mark.parametrize("kind", ["timeout", "truncated", "wrong-model"])
def test_unusable_outputs_keep_evidence_without_proxy_score(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(smoke, "load_eval_set", items)

    def provider(*args, **kwargs):
        if kind == "timeout":
            raise DeploymentTransientError("request-timeout", request_attempted=True)
        return {"text": "Billing", "reported_model_id": "other" if kind == "wrong-model" else "exact:tag",
                "completion_reason": "length" if kind == "truncated" else "stop", "output_tokens": 512}

    result = smoke.evaluate_smoke(registry(), "edge", ["small"], output=tmp_path / "smoke.json",
                                  dispatcher=provider, progress=False)
    record = result["records"][0]
    assert record["score"] is None and record["error_code"]
    if kind != "timeout":
        assert record["result"]["text"] == "Billing"
