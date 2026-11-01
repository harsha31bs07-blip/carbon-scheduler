"""Provider nanosecond timings remain explicit, finite and separate from latency."""

import math
import traceback

import pytest

from shared.deployments import Deployment
from workflow import deployment_clients as clients


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body
        self.closed = False

    def json(self):
        return self.body

    def close(self):
        self.closed = True


def call_ollama(monkeypatch, metadata):
    response = Response({"model": "llama:exact", "response": "answer", "done": True,
                         "done_reason": "stop", **metadata})
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return response

    monkeypatch.setattr(clients.requests, "post", post)
    monkeypatch.setattr(clients.time, "sleep", lambda *_: pytest.fail("Malformed timing should not retry"))
    deployment = Deployment("local", "ollama", "http://localhost:11434", {"small": "llama:exact"})
    return deployment, response, calls


def test_complete_provider_timings_convert_nanoseconds_without_replacing_wall_latency(monkeypatch):
    deployment, response, calls = call_ollama(monkeypatch, {
        "load_duration": 2_000_000_000, "total_duration": 3_500_000_000,
        "prompt_eval_duration": 0, "eval_duration": 1_234_567_890,
    })
    result = clients.call_deployment(deployment, "small", "hello")
    assert result["provider_timings_seconds"] == {"load": 2.0, "total": 3.5, "prefill": 0.0, "decode": 1.23456789}
    assert math.isfinite(result["latency_ms"]) and result["latency_ms"] >= 0
    assert "latency_ms" not in result["provider_timings_seconds"]
    assert calls == ["http://localhost:11434/api/generate"] and response.closed


def test_missing_or_null_duration_metadata_stays_missing_not_zero(monkeypatch):
    deployment, response, _ = call_ollama(monkeypatch, {"load_duration": None, "unrelated_duration": 5})
    result = clients.call_deployment(deployment, "small", "hello")
    assert result["provider_timings_seconds"] == {} and response.closed


@pytest.mark.parametrize("field", ["load_duration", "total_duration", "prompt_eval_duration", "eval_duration"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "provider-private-timing", [], {}, float("nan"), float("inf"), 10 ** 1000])
def test_invalid_timing_metadata_fails_safely_after_one_request(monkeypatch, field, value):
    deployment, response, calls = call_ollama(monkeypatch, {field: value})
    with pytest.raises(clients.DeploymentCallError) as raised:
        clients.call_deployment(deployment, "small", "hello", max_attempts=3)
    assert raised.value.error_code == "invalid-provider-timing"
    assert raised.value.request_attempted and raised.value.attempt_count == 1
    assert not raised.value.retryable and response.closed and len(calls) == 1
    assert "provider-private-timing" not in "".join(traceback.format_exception(raised.value))
