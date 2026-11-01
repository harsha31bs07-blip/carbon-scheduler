"""Explicit inference destinations, independent of model and grid identity.

Configuration describes intended capabilities, never verified availability. A
separate timestamped health observation supplies that evidence. Loading this
module or a registry makes no network calls and does not read credential values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from urllib.parse import urlsplit


REGISTRY_SCHEMA_VERSION = 1
PROVIDERS = frozenset({"ollama", "gemini", "nvidia"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a nonempty identifier of at most 128 characters")


def _text(value: str, name: str) -> None:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"{name} must be nonempty text without surrounding whitespace or controls")


def _endpoint(value: str) -> None:
    # Reject all query strings: provider credentials belong in request headers,
    # and a config snapshot must remain safe to persist or display.
    if (not isinstance(value, str) or not value or "?" in value or "#" in value
            or "\\" in value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("endpoint must be an HTTP(S) URL without credentials, query, or fragment")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None)
        port = parsed.port  # Access validates malformed and out-of-range ports.
    except ValueError:
        raise ValueError("endpoint must be a valid HTTP(S) URL") from None
    if not valid or port == 0:
        raise ValueError("endpoint must be an HTTP(S) URL without credentials")


def _fingerprint(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Deployment:
    """A configured endpoint; ``models`` maps canonical IDs to exact provider IDs.

    ``None`` means unknown location/profile, including for local endpoints. A
    configured region/grid requires its provenance; it is never inferred from a
    provider name, URL, credential, or the coordinator's current location.
    """

    deployment_id: str
    provider: str
    endpoint: str
    models: Mapping[str, str]
    hardware_profile_id: str | None = None
    credential_env: str | None = None
    region: str | None = None
    grid: str | None = None
    location_source: str | None = None
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        _identifier(self.deployment_id, "deployment_id")
        if not isinstance(self.provider, str) or self.provider not in PROVIDERS:
            raise ValueError("provider must be ollama, gemini, or nvidia")
        _endpoint(self.endpoint)
        if not isinstance(self.models, Mapping) or not self.models:
            raise ValueError("models must be a nonempty canonical-to-provider mapping")
        model_copy = dict(self.models)
        for canonical, provider_id in model_copy.items():
            _identifier(canonical, "canonical model ID")
            _text(provider_id, "provider model ID")
            if any(char.isspace() for char in provider_id):
                raise ValueError("provider model ID cannot contain whitespace")
        object.__setattr__(self, "models", MappingProxyType(model_copy))
        if self.hardware_profile_id is not None:
            _identifier(self.hardware_profile_id, "hardware_profile_id")
        if self.credential_env is not None and (
            not isinstance(self.credential_env, str) or not _ENV_NAME.fullmatch(self.credential_env)
        ):
            raise ValueError("credential_env must name an environment variable, not contain its value")
        for name in ("region", "grid", "location_source"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)
        if (self.region is not None or self.grid is not None) and self.location_source is None:
            raise ValueError("configured geography requires location_source")
        if type(self.max_concurrency) is not int or self.max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")

    def provider_model(self, model_id: str) -> str:
        """Resolve only explicitly supported pairs, with no alias guessing."""
        _identifier(model_id, "canonical model ID")
        try:
            return self.models[model_id]
        except KeyError:
            raise ValueError("model is not configured on the selected deployment") from None

    def to_dict(self) -> dict:
        return {field.name: (dict(self.models) if field.name == "models" else getattr(self, field.name))
                for field in fields(self)}

    @classmethod
    def from_dict(cls, value: Mapping) -> Deployment:
        if not isinstance(value, Mapping):
            raise ValueError("deployment must be an object")
        if set(value) - {field.name for field in fields(cls)}:
            # Do not echo unsupported fields or their values: they may be secrets.
            raise ValueError("deployment contains unsupported fields")
        try:
            return cls(**value)
        except TypeError:
            raise ValueError("deployment is missing required fields") from None

    def config_fingerprint(self) -> str:
        return _fingerprint(self.to_dict())


class DeploymentRegistry:
    """Validated configuration inventory. Membership is not a health assertion."""

    def __init__(self, deployments: Iterable[Deployment]) -> None:
        indexed: dict[str, Deployment] = {}
        for deployment in deployments:
            if not isinstance(deployment, Deployment):
                raise ValueError("registry entries must be Deployment instances")
            if deployment.deployment_id in indexed:
                raise ValueError("duplicate deployment_id in registry")
            indexed[deployment.deployment_id] = deployment
        self._deployments = MappingProxyType(indexed)

    def __iter__(self):
        return iter(self._deployments.values())

    def __len__(self) -> int:
        return len(self._deployments)

    def get(self, deployment_id: str) -> Deployment:
        _identifier(deployment_id, "deployment_id")
        try:
            return self._deployments[deployment_id]
        except KeyError:
            raise ValueError("deployment_id is not configured") from None

    def require_model(self, deployment_id: str, model_id: str) -> Deployment:
        deployment = self.get(deployment_id)
        deployment.provider_model(model_id)
        return deployment

    def for_model(self, model_id: str) -> tuple[Deployment, ...]:
        _identifier(model_id, "canonical model ID")
        return tuple(deployment for deployment in self if model_id in deployment.models)

    def to_dict(self) -> dict:
        return {"schema_version": REGISTRY_SCHEMA_VERSION,
                "deployments": [deployment.to_dict() for deployment in self]}

    def config_fingerprint(self) -> str:
        value = self.to_dict()
        value["deployments"].sort(key=lambda deployment: deployment["deployment_id"])
        return _fingerprint(value)

    @classmethod
    def from_dict(cls, value: Mapping) -> DeploymentRegistry:
        if not isinstance(value, Mapping) or set(value) != {"schema_version", "deployments"}:
            raise ValueError("registry requires schema_version and deployments only")
        if type(value["schema_version"]) is not int or value["schema_version"] != REGISTRY_SCHEMA_VERSION:
            raise ValueError("unsupported deployment registry schema_version")
        if not isinstance(value["deployments"], list):
            raise ValueError("deployments must be a list")
        return cls(Deployment.from_dict(item) for item in value["deployments"])

    @classmethod
    def load(cls, path: str | Path) -> DeploymentRegistry:
        def unique_object(pairs: list[tuple[str, object]]) -> dict:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate field in deployment configuration")
                result[key] = value
            return result

        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8-sig"),
                                 object_pairs_hook=unique_object)
        except json.JSONDecodeError:
            raise ValueError("deployment configuration is not valid JSON") from None
        return cls.from_dict(payload)


def load_deployments(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> DeploymentRegistry:
    """Load explicit JSON or a local-only default, without probing any endpoints.

    ``CARBON_DEPLOYMENTS_FILE`` selects a registry when ``path`` is omitted.
    Cloud deployments require explicit configuration even if keys are present.
    Credentials are resolved by transport code only when making a request.
    """
    env = os.environ if environ is None else environ
    config_path = path if path is not None else env.get("CARBON_DEPLOYMENTS_FILE")
    if config_path is not None:
        return DeploymentRegistry.load(config_path)
    return DeploymentRegistry([Deployment(
        deployment_id="local-ollama", provider="ollama",
        endpoint=env.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        models={"local-qwen3.5": "qwen3.5:4b", "local-llama3.2": "llama3.2:3b"},
    )])


@dataclass(frozen=True)
class DeploymentHealth:
    """Observed capabilities at a known time, separate from registry claims.

    ``reachable=None`` means that reachability has not been observed. An empty
    ``available_models`` in that state makes no claim about deployed models.
    Otherwise ``available_models`` contains canonical IDs actually observed.
    ``error_code`` is a safe machine-readable code; raw provider messages must
    not be stored.
    """

    deployment_id: str
    checked_at: datetime
    reachable: bool | None
    available_models: tuple[str, ...]
    source: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.deployment_id, "deployment_id")
        if (not isinstance(self.checked_at, datetime) or self.checked_at.tzinfo is None
                or self.checked_at.utcoffset() is None):
            raise ValueError("checked_at must be a timezone-aware datetime")
        object.__setattr__(self, "checked_at", self.checked_at.astimezone(timezone.utc))
        if self.reachable is not None and type(self.reachable) is not bool:
            raise ValueError("reachable must be a boolean or null for unknown")
        if not isinstance(self.available_models, (tuple, list)):
            raise ValueError("available_models must be a list or tuple of canonical IDs")
        models = tuple(self.available_models)
        for model in models:
            _identifier(model, "observed model ID")
        if len(set(models)) != len(models):
            raise ValueError("duplicate observed model IDs")
        if models and self.reachable is not True:
            raise ValueError("unreachable or unprobed deployment cannot report available models")
        object.__setattr__(self, "available_models", models)
        _identifier(self.source, "health source")
        if self.error_code is not None:
            _identifier(self.error_code, "error_code")

    def to_dict(self) -> dict:
        return {"deployment_id": self.deployment_id,
                "checked_at": self.checked_at.isoformat().replace("+00:00", "Z"),
                "reachable": self.reachable, "available_models": list(self.available_models),
                "source": self.source, "error_code": self.error_code}

    @classmethod
    def from_dict(cls, value: Mapping) -> DeploymentHealth:
        if not isinstance(value, Mapping) or set(value) != {field.name for field in fields(cls)}:
            raise ValueError("health observation requires all supported fields only")
        checked_at = value["checked_at"]
        if not isinstance(checked_at, str):
            raise ValueError("checked_at must be an ISO 8601 timestamp")
        try:
            checked_at = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("checked_at must be an ISO 8601 timestamp") from None
        return cls(**{**value, "checked_at": checked_at})

