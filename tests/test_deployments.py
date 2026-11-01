"""Deployment configuration is explicit, immutable, and safe to record offline."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from shared.deployments import Deployment, DeploymentHealth, DeploymentRegistry, load_deployments


def deployment(**changes):
    values = {"deployment_id": "worker-a", "provider": "ollama",
              "endpoint": "http://worker-a:11434", "models": {"local-qwen3.5": "qwen3.5:4b"}}
    values.update(changes)
    return Deployment(**values)


def test_registry_routes_identity_to_supported_provider_model():
    first = deployment()
    second = deployment(deployment_id="worker-b", endpoint="http://worker-b:11434",
                        models={"local-llama3.2": "llama3.2:3b"})
    registry = DeploymentRegistry([first, second])
    assert registry.require_model("worker-a", "local-qwen3.5") is first
    assert registry.get("worker-b").provider_model("local-llama3.2") == "llama3.2:3b"
    assert registry.for_model("local-qwen3.5") == (first,)
    assert registry.for_model("unsupported") == ()
    with pytest.raises(ValueError, match="not configured"):
        registry.require_model("worker-a", "local-llama3.2")
    with pytest.raises(ValueError, match="not configured"):
        registry.get("unknown")


def test_unknown_geography_is_not_inferred_from_url_or_provider():
    configured = deployment(provider="gemini", endpoint="https://us-central1.example.test",
                            credential_env="GOOGLE_API_KEY")
    assert configured.region is None
    assert configured.grid is None
    assert configured.location_source is None
    assert Deployment.from_dict(configured.to_dict()) == configured


def test_geography_requires_provenance_but_does_not_require_every_field_known():
    with pytest.raises(ValueError, match="location_source"):
        deployment(region="region-a")
    with pytest.raises(ValueError, match="location_source"):
        deployment(grid="grid-a")
    configured = deployment(grid="grid-a", location_source="operator-verified")
    assert configured.grid == "grid-a"
    assert configured.region is None


@pytest.mark.parametrize("endpoint", [
    "ftp://worker-a", "worker-a:11434", "http://", "http:///worker-a", "http://worker-a:0",
    "https://worker-a:99999", "https://worker-a:abc", "https://[bad-ipv6",
    "https://user:password@worker-a", "https://token@worker-a", "https://worker-a?key=secret",
    "https://worker-a?api-version=1", "https://worker-a?", "https://worker-a#secret",
    "https://worker-a#", " https://worker-a", "https://worker\na", "https://worker-a\\path",
    "https://worker-a\x7f", None, 123,
])
def test_unsafe_or_invalid_endpoint_rejected_without_echo(endpoint):
    with pytest.raises(ValueError, match="endpoint") as error:
        deployment(endpoint=endpoint)
    assert str(endpoint) not in str(error.value)


@pytest.mark.parametrize("changes", [
    {"deployment_id": ""}, {"deployment_id": "contains spaces"}, {"provider": "unknown"},
    {"models": {}}, {"models": ["local-qwen3.5"]}, {"models": {"local-qwen3.5": ""}},
    {"models": {"local-qwen3.5": "qwen 4b"}}, {"models": {"bad alias": "provider-model"}},
    {"credential_env": "Bearer secret"}, {"credential_env": "KEY=secret"},
    {"credential_env": ""}, {"max_concurrency": True}, {"max_concurrency": 0},
    {"max_concurrency": 1.5}, {"hardware_profile_id": "bad profile"}, {"region": ""},
])
def test_invalid_deployment_configuration_rejected(changes):
    with pytest.raises(ValueError):
        deployment(**changes)


def test_model_configuration_is_copied_and_immutable():
    aliases = {"local-qwen3.5": "qwen3.5:4b"}
    configured = deployment(models=aliases)
    aliases["local-qwen3.5"] = "changed"
    assert configured.provider_model("local-qwen3.5") == "qwen3.5:4b"
    with pytest.raises(TypeError):
        configured.models["local-qwen3.5"] = "changed"
    exported = configured.to_dict()
    exported["models"]["local-qwen3.5"] = "changed"
    assert configured.provider_model("local-qwen3.5") == "qwen3.5:4b"


def test_duplicate_deployment_ids_rejected():
    with pytest.raises(ValueError, match="duplicate deployment_id"):
        DeploymentRegistry([deployment(), deployment(endpoint="http://worker-b:11434")])


def test_json_rejects_duplicate_model_aliases(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"deployments":[{"models":{"same":"a","same":"b"}}]}')
    with pytest.raises(ValueError, match="duplicate field"):
        DeploymentRegistry.load(path)


def test_json_rejects_duplicate_deployment_fields(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"deployments":['
                    '{"deployment_id":"a","deployment_id":"b"}]}')
    with pytest.raises(ValueError, match="duplicate field"):
        DeploymentRegistry.load(path)


@pytest.mark.parametrize("invalid_id", [None, [], 1, "", "contains spaces"])
def test_lookup_ids_are_validated(invalid_id):
    configured = deployment()
    registry = DeploymentRegistry([configured])
    for lookup in (configured.provider_model, registry.get, registry.for_model):
        with pytest.raises(ValueError):
            lookup(invalid_id)


def test_unknown_config_fields_are_rejected_without_echoing_credentials():
    value = deployment().to_dict()
    value["api_key"] = "do-not-print-secret"
    with pytest.raises(ValueError, match="unsupported fields") as error:
        Deployment.from_dict(value)
    assert "do-not-print-secret" not in str(error.value)


@pytest.mark.parametrize("payload", [
    [], {}, {"schema_version": 2, "deployments": []}, {"schema_version": True, "deployments": []},
    {"schema_version": 1, "deployments": {}},
    {"schema_version": 1, "deployments": [], "credential": "secret"},
    {"schema_version": 1, "deployments": [{}]},
])
def test_registry_schema_is_explicit(payload):
    with pytest.raises(ValueError):
        DeploymentRegistry.from_dict(payload)


def test_fingerprint_tracks_configuration_but_never_credential_values(monkeypatch):
    first = deployment(credential_env="WORKER_API_KEY")
    second = deployment(deployment_id="worker-b", endpoint="http://worker-b:11434")
    registry = DeploymentRegistry([first, second])
    monkeypatch.setenv("WORKER_API_KEY", "do-not-serialize-secret")
    before = registry.config_fingerprint()
    assert "do-not-serialize-secret" not in json.dumps(registry.to_dict())
    monkeypatch.setenv("WORKER_API_KEY", "rotated-secret")
    assert registry.config_fingerprint() == before
    assert DeploymentRegistry([second, first]).config_fingerprint() == before
    changed = deployment(endpoint="http://worker-c:11434", credential_env="WORKER_API_KEY")
    assert changed.config_fingerprint() != first.config_fingerprint()
    assert DeploymentRegistry([changed, second]).config_fingerprint() != before


def test_file_loading_and_environment_selection(tmp_path):
    configured = DeploymentRegistry([deployment()])
    path = tmp_path / "deployments.json"
    path.write_text(json.dumps(configured.to_dict()), encoding="utf-8")
    loaded = load_deployments(environ={"CARBON_DEPLOYMENTS_FILE": str(path)})
    assert loaded.config_fingerprint() == configured.config_fingerprint()
    assert load_deployments(path, environ={"CARBON_DEPLOYMENTS_FILE": "missing"}).to_dict() == loaded.to_dict()


def test_default_registry_does_not_promote_keys_to_verified_cloud_capability():
    registry = load_deployments(environ={"GOOGLE_API_KEY": "secret", "NVIDIA_API_KEY": "secret"})
    assert len(registry) == 1
    local = registry.get("local-ollama")
    assert local.endpoint == "http://localhost:11434"
    assert local.region is local.grid is local.location_source is None
    assert local.provider_model("local-qwen3.5") == "qwen3.5:4b"


def test_default_endpoint_override_does_not_imply_local_geography():
    registry = load_deployments(environ={"OLLAMA_BASE_URL": "http://remote-worker:11434"})
    configured = registry.get("local-ollama")
    assert configured.endpoint == "http://remote-worker:11434"
    assert configured.region is None
    assert configured.hardware_profile_id is None


def test_example_is_valid_and_does_not_claim_configured_models_are_available():
    registry = DeploymentRegistry.load(Path(__file__).resolve().parents[1] / "config/deployments.example.json")
    assert registry.get("local-ollama").provider == "ollama"
    assert "health" not in registry.to_dict()


def test_health_snapshot_records_actual_capability_at_utc_time():
    observed = datetime(2026, 10, 18, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    health = DeploymentHealth("worker-a", observed, True, ("local-qwen3.5",), "ollama-tags")
    assert health.to_dict() == {"deployment_id": "worker-a", "checked_at": "2026-10-18T06:30:00Z",
                                "reachable": True, "available_models": ["local-qwen3.5"],
                                "source": "ollama-tags", "error_code": None}
    assert DeploymentHealth.from_dict(health.to_dict()) == health


def test_failed_health_observation_does_not_claim_available_models():
    with pytest.raises(ValueError, match="unreachable"):
        DeploymentHealth("worker-a", datetime.now(timezone.utc), False, ("local-qwen3.5",), "ollama-tags")
    health = DeploymentHealth("worker-a", datetime.now(timezone.utc), False, (), "ollama-tags", "timeout")
    assert health.to_dict()["error_code"] == "timeout"
    with pytest.raises(ValueError, match="timezone-aware"):
        DeploymentHealth("worker-a", datetime(2026, 10, 18), True, (), "ollama-tags")


def test_unprobed_health_preserves_unknown_reachability():
    health = DeploymentHealth("worker-a", datetime.now(timezone.utc), None, (), "not-probed",
                              "verification-not-performed")
    assert health.to_dict()["reachable"] is None
    assert DeploymentHealth.from_dict(health.to_dict()) == health
    with pytest.raises(ValueError, match="unprobed"):
        DeploymentHealth("worker-a", datetime.now(timezone.utc), None, ("local-qwen3.5",),
                         "not-probed")


@pytest.mark.parametrize("changes", [
    {"reachable": 1}, {"reachable": "false"}, {"available_models": "local-qwen3.5"},
    {"available_models": ["local-qwen3.5", "local-qwen3.5"]},
    {"source": "Bearer secret"}, {"error_code": "provider message with token"},
    {"checked_at": "invalid"}, {"checked_at": 1}, {"checked_at": "2026-10-18T12:00:00"},
    {"unsupported": "do-not-print-secret"},
])
def test_invalid_health_payload_rejected_without_values(changes):
    payload = DeploymentHealth("worker-a", datetime.now(timezone.utc), True, (),
                               "ollama-tags").to_dict()
    payload.update(changes)
    with pytest.raises(ValueError) as error:
        DeploymentHealth.from_dict(payload)
    assert "do-not-print-secret" not in str(error.value)
