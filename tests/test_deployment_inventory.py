"""Health must describe an observation, not inferred provider availability."""

import json

import pytest
import requests

from shared.deployments import Deployment, DeploymentRegistry
from workflow import deployment_inventory as inventory


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status
        self.closed = False

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network request")
    monkeypatch.setattr(inventory.requests, "get", forbidden)
    monkeypatch.setattr(inventory.requests, "post", forbidden)


def ollama():
    return Deployment("local", "ollama", "http://localhost:11434",
                      {"small": "qwen:4b", "large": "qwen:8b"})


def test_tags_probe_reports_only_observed_exact_model_ids(monkeypatch):
    calls = []
    response = Response({"models": [{"name": "qwen:4b", "model": "qwen:4b"},
                                     {"name": "other:latest"}]})

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(inventory.requests, "get", get)
    health = inventory.probe_deployment(ollama(), timeout_seconds=2)
    assert calls == [("http://localhost:11434/api/tags", {
        "headers": {}, "timeout": (2.0, 2.0), "allow_redirects": False,
    })]
    assert health.reachable is True
    assert health.available_models == ("small",)
    assert health.checked_at.utcoffset().total_seconds() == 0
    assert health.source == "ollama-tags" and health.error_code is None
    assert response.closed is True


def test_cloud_key_presence_never_asserts_reachability(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "secret-key")
    deployment = Deployment("gemini", "gemini", "https://generativelanguage.googleapis.com",
                            {"large": "gemini-version"}, credential_env="GOOGLE_API_KEY")
    health = inventory.probe_deployment(deployment)
    assert health.reachable is None and health.available_models == ()
    assert health.source == "not-probed"
    assert health.error_code == "verification-not-performed"


@pytest.mark.parametrize("key,error_code", [(None, "missing-credential"), ("", "invalid-credential")])
def test_credential_preflight_keeps_reachability_unknown(monkeypatch, key, error_code):
    deployment = Deployment("protected", "ollama", "https://ollama.example",
                            {"small": "qwen:4b"}, credential_env="OLLAMA_TEST_KEY")
    if key is None:
        monkeypatch.delenv("OLLAMA_TEST_KEY", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_TEST_KEY", key)
    health = inventory.probe_deployment(deployment)
    assert health.reachable is None and health.available_models == ()
    assert health.source == "credential-preflight"
    assert health.error_code == error_code


@pytest.mark.parametrize("status,error_code", [
    (401, "authentication-rejected"), (403, "authentication-rejected"),
    (302, "http-request-rejected"), (404, "http-request-rejected"),
    (503, "http-request-rejected"),
])
def test_http_rejection_proves_reachability_but_no_model_capability(monkeypatch, status, error_code):
    response = Response({"error": "secret-provider-message"}, status=status)
    monkeypatch.setattr(inventory.requests, "get", lambda *_a, **_k: response)
    health = inventory.probe_deployment(ollama())
    assert health.reachable is True and health.available_models == ()
    assert health.source == "ollama-tags" and health.error_code == error_code
    assert "secret-provider-message" not in str(health.to_dict())
    assert response.closed is True


@pytest.mark.parametrize("failure, error_code", [
    (requests.exceptions.Timeout("provider secret-key"), "request-timeout"),
    (requests.exceptions.ConnectionError("http://secret-key"), "connection-failed"),
])
def test_transport_failure_safe(monkeypatch, failure, error_code):
    def get(*args, **kwargs):
        raise failure
    monkeypatch.setattr(inventory.requests, "get", get)
    health = inventory.probe_deployment(ollama())
    assert health.reachable is False and health.available_models == ()
    assert health.error_code == error_code
    assert "secret-key" not in str(health.to_dict())


@pytest.mark.parametrize("body", [
    {"models": "invalid"}, {"models": [False]}, {"models": [{"name": 4}]},
    ValueError("secret-json-error"),
    requests.exceptions.JSONDecodeError("secret-json-error", "secret-provider-body", 0),
])
def test_invalid_tags_response_does_not_claim_capability(monkeypatch, body):
    monkeypatch.setattr(inventory.requests, "get", lambda *_a, **_k: Response(body))
    health = inventory.probe_deployment(ollama())
    assert health.reachable is True and health.available_models == ()
    assert health.error_code == "invalid-response"


def test_cli_outputs_json_inventory_without_credentials(monkeypatch, capsys):
    registry = DeploymentRegistry([ollama()])
    monkeypatch.setattr(inventory, "load_deployments", lambda path: registry)
    monkeypatch.setattr(inventory.requests, "get", lambda *_a, **_k: Response({"models": []}))
    assert inventory.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["configuration_fingerprint"] == registry.config_fingerprint()
    assert output["deployments"][0]["health"]["reachable"] is True
    assert output["deployments"][0]["health"]["available_models"] == []
    assert output["deployments"][0]["configuration"]["region"] is None


def test_cli_invalid_config_error_is_json_and_safe(monkeypatch, capsys):
    def load(path):
        raise ValueError("secret-config-value")
    monkeypatch.setattr(inventory, "load_deployments", load)
    assert inventory.main(["--config", "bad.json"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error_code": "invalid-inventory-configuration"}
